//! Stateful mono speech enhancement backed by the authors' pretrained DFN3 runtime.
//! Each input stream owns its model and resampling state. Audio is normalized PCM.
use anyhow::{ensure, Context, Result};
use df::tract::{DfParams, DfTract, RuntimeParams};
use ndarray::{ArrayView2, ArrayViewMut2};

mod telephony;
use telephony::{TelephoneResampler, CONVERSION_DELAY_SAMPLES, RATIO};

pub const SAMPLE_RATE: u32 = 48_000;
pub const TELEPHONY_SAMPLE_RATE: u32 = 8_000;

/// A single speech stream. The underlying runtime retains recurrent and DSP state.
pub struct Denoiser {
    model: DfTract,
}

/// A 10 ms mono stream at 8 or 48 kHz, with independent model and conversion state.
pub struct StreamDenoiser {
    model: Denoiser,
    sample_rate: u32,
    telephone: Option<TelephoneStream>,
}

struct TelephoneStream {
    resampler: TelephoneResampler,
    input: [f32; 480],
    output: [f32; 480],
    dry: Option<Vec<f32>>,
    dry_head: usize,
}

impl StreamDenoiser {
    /// Construct off the audio thread. The model always runs internally at 48 kHz.
    pub fn new(sample_rate: u32, atten_lim_db: f32) -> Result<Self> {
        ensure!(
            matches!(sample_rate, TELEPHONY_SAMPLE_RATE | SAMPLE_RATE),
            "supported sample rates are 8000 and 48000 Hz"
        );
        let model = Denoiser::new(atten_lim_db)?;
        let telephone = if sample_rate == TELEPHONY_SAMPLE_RATE {
            ensure!(
                model.delay_samples() % RATIO == 0,
                "model delay is not integral at 8 kHz"
            );
            let delay = model.delay_samples() / RATIO + CONVERSION_DELAY_SAMPLES;
            Some(TelephoneStream {
                resampler: TelephoneResampler::new(),
                input: [0.0; 480],
                output: [0.0; 480],
                dry: (atten_lim_db == 0.0).then(|| vec![0.0; delay]),
                dry_head: 0,
            })
        } else {
            None
        };
        Ok(Self {
            model,
            sample_rate,
            telephone,
        })
    }

    pub fn sample_rate(&self) -> u32 {
        self.sample_rate
    }

    pub fn frame_samples(&self) -> usize {
        self.sample_rate as usize / 100
    }

    /// Alignment delay in interface samples: 304 at 8 kHz, 1,440 at 48 kHz.
    pub fn delay_samples(&self) -> usize {
        if self.telephone.is_some() {
            self.model.delay_samples() / RATIO + CONVERSION_DELAY_SAMPLES
        } else {
            self.model.delay_samples()
        }
    }

    pub fn process_frame(&mut self, input: &[f32], output: &mut [f32]) -> Result<()> {
        let hop = self.frame_samples();
        ensure!(
            input.len() == hop && output.len() == hop,
            "expected {hop} samples per frame"
        );
        ensure!(
            input.iter().all(|v| v.is_finite() && v.abs() <= 1.0),
            "input must be finite normalized PCM in [-1, 1]"
        );
        if let Some(telephone) = &mut self.telephone {
            telephone.resampler.upsample(input, &mut telephone.input);
            // Band-limited interpolation can overshoot full-scale transients.
            for sample in &mut telephone.input {
                *sample = sample.clamp(-1.0, 1.0);
            }
            self.model
                .process_frame(&telephone.input, &mut telephone.output)?;
            telephone.resampler.downsample(&telephone.output, output);
            ensure!(
                output.iter().all(|v| v.is_finite()),
                "resampler produced non-finite output"
            );
            if let Some(dry) = &mut telephone.dry {
                for (&sample, destination) in input.iter().zip(output) {
                    *destination = dry[telephone.dry_head];
                    dry[telephone.dry_head] = sample;
                    telephone.dry_head = (telephone.dry_head + 1) % dry.len();
                }
            }
            Ok(())
        } else {
            self.model.process_frame(input, output)
        }
    }
}

impl Denoiser {
    /// Limit suppression to `atten_lim_db` (0 = dry, 100 = unlimited suppression).
    /// The conservative default used by the CLI is 24 dB. Construct off the audio thread.
    pub fn new(atten_lim_db: f32) -> Result<Self> {
        ensure!(
            atten_lim_db.is_finite() && (0.0..=100.0).contains(&atten_lim_db),
            "attenuation must be finite and between 0 and 100 dB"
        );
        let params = RuntimeParams::default_with_ch(1).with_atten_lim(atten_lim_db);
        let model = DfTract::new(DfParams::default(), &params)
            .context("could not initialize embedded DeepFilterNet3 model")?;
        ensure!(model.sr == SAMPLE_RATE as usize, "expected a 48 kHz model");
        ensure!(model.hop_size == 480, "expected a 10 ms model hop");
        ensure!(model.ch == 1, "expected a mono model");
        Ok(Self { model })
    }

    pub fn frame_samples(&self) -> usize {
        self.model.hop_size
    }

    /// Signal alignment delay, excluding the time required to collect the current hop.
    /// For the pinned DFN3 model this is 1,440 samples (30 ms); frame acquisition adds
    /// up to 10 ms, corresponding to the paper's 40 ms algorithmic latency.
    pub fn delay_samples(&self) -> usize {
        self.model.fft_size - self.model.hop_size + self.model.lookahead * self.model.hop_size
    }

    /// Process exactly one hop. Invalid input is rejected before state is advanced.
    /// A runtime error makes this stream unusable; construct a fresh instance to reset.
    pub fn process_frame(&mut self, input: &[f32], output: &mut [f32]) -> Result<()> {
        let hop = self.frame_samples();
        ensure!(
            input.len() == hop && output.len() == hop,
            "expected {hop} samples per frame"
        );
        ensure!(
            input.iter().all(|v| v.is_finite() && v.abs() <= 1.0),
            "input must be finite normalized PCM in [-1, 1]"
        );
        let input = ArrayView2::from_shape((1, hop), input)?;
        let target = ArrayViewMut2::from_shape((1, hop), &mut *output)?;
        self.model
            .process(input, target)
            .context("DeepFilterNet3 inference failed")?;
        ensure!(
            output.iter().all(|v| v.is_finite()),
            "model produced non-finite output"
        );
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stream_sample_rates_are_explicit_and_validated_before_model_loading() {
        for rate in [0, 8, 16_000, 44_100, 96_000] {
            assert!(StreamDenoiser::new(rate, 24.0).is_err());
        }
    }

    #[test]
    fn telephone_framing_validation_silence_and_stream_state() -> Result<()> {
        let mut actual = StreamDenoiser::new(8_000, 24.0)?;
        let mut fresh = StreamDenoiser::new(8_000, 24.0)?;
        assert_eq!(actual.sample_rate(), 8_000);
        assert_eq!(actual.frame_samples(), 80);
        assert_eq!(actual.delay_samples(), 304);
        let mut output = [0.0; 80];
        let mut expected = [0.0; 80];
        assert!(actual.process_frame(&[0.0; 79], &mut output).is_err());
        assert!(actual.process_frame(&[f32::NAN; 80], &mut output).is_err());
        assert!(actual
            .process_frame(&[f32::INFINITY; 80], &mut output)
            .is_err());
        assert!(actual.process_frame(&[1.1; 80], &mut output).is_err());
        for _ in 0..5 {
            actual.process_frame(&[0.0; 80], &mut output)?;
            fresh.process_frame(&[0.0; 80], &mut expected)?;
            assert_eq!(output, expected);
            assert!(output.iter().all(|v| v.abs() < 1e-7));
        }
        let mut energy = 0.0;
        for frame in 0..20 {
            let input: Vec<f32> = (0..80)
                .map(|i| match frame {
                    2..=4 => 0.0,
                    5 => {
                        if i < 40 {
                            -1.0
                        } else {
                            1.0
                        }
                    }
                    _ => 0.25 * ((frame * 80 + i) as f32 * 0.3).sin(),
                })
                .collect();
            actual.process_frame(&input, &mut output)?;
            fresh.process_frame(&input, &mut expected)?;
            assert_eq!(output, expected);
            assert!(output.iter().all(|v| v.is_finite()));
            energy += output.iter().map(|v| v * v).sum::<f32>();
        }
        assert!(energy > 0.01, "telephone-band signal was discarded");
        Ok(())
    }

    #[test]
    fn telephone_dry_mode_preserves_pcm_at_total_conversion_delay() -> Result<()> {
        let mut denoiser = StreamDenoiser::new(8_000, 0.0)?;
        let source: Vec<f32> = (0..1_600)
            .map(|i| match i / 80 {
                3..=6 => 0.0,
                7 => {
                    if i % 2 == 0 {
                        -1.0
                    } else {
                        32767.0 / 32768.0
                    }
                }
                _ => ((i as f32 * 0.21).sin() * 16_000.0).round() / 32768.0,
            })
            .collect();
        let delay = denoiser.delay_samples();
        let mut padded = source.clone();
        padded.resize((source.len() + delay).div_ceil(80) * 80, 0.0);
        let mut output = vec![0.0; padded.len()];
        for (input, output) in padded.chunks(80).zip(output.chunks_mut(80)) {
            denoiser.process_frame(input, output)?;
        }
        assert!(output[..delay].iter().all(|&v| v == 0.0));
        assert_eq!(&output[delay..delay + source.len()], &source);
        Ok(())
    }

    #[test]
    fn full_band_wrapper_preserves_existing_model_samples_exactly() -> Result<()> {
        let mut original = Denoiser::new(24.0)?;
        let mut stream = StreamDenoiser::new(48_000, 24.0)?;
        assert_eq!(stream.frame_samples(), original.frame_samples());
        assert_eq!(stream.delay_samples(), original.delay_samples());
        let mut expected = [0.0; 480];
        let mut actual = [0.0; 480];
        for frame in 0..8 {
            let input: Vec<f32> = (0..480)
                .map(|i| {
                    if frame == 3 {
                        0.0
                    } else {
                        0.3 * ((frame * 480 + i) as f32 * 0.11).sin()
                    }
                })
                .collect();
            original.process_frame(&input, &mut expected)?;
            stream.process_frame(&input, &mut actual)?;
            assert_eq!(actual, expected);
        }
        Ok(())
    }

    #[test]
    fn stft_reset_and_clone_preserve_stream_isolation() {
        let mut reused = df::DFState::default();
        let mut fresh = df::DFState::default();
        let mut reused_output = vec![0.0; reused.frame_size];
        let mut fresh_output = vec![0.0; fresh.frame_size];
        let warmup: Vec<f32> = (0..reused.frame_size)
            .map(|i| (i as f32 * 0.13).sin() * 0.4)
            .collect();
        for _ in 0..7 {
            reused.process_frame(&warmup, &mut reused_output);
        }
        // Reused FFT workspaces retain their old contents across reset. None of
        // those samples may leak into a newly reset stream.
        reused.reset();
        for frame in 0..9 {
            let input: Vec<f32> = (0..reused.frame_size)
                .map(|i| ((frame * reused.frame_size + i) as f32 * 0.07).sin() * 0.2)
                .collect();
            reused.process_frame(&input, &mut reused_output);
            fresh.process_frame(&input, &mut fresh_output);
            assert_eq!(reused_output, fresh_output);
        }

        let mut fork = reused.clone();
        let mut fork_output = vec![0.0; fork.frame_size];
        for _ in 0..4 {
            reused.process_frame(&warmup, &mut reused_output);
            fork.process_frame(&warmup, &mut fork_output);
            assert_eq!(reused_output, fork_output);
        }
    }

    #[test]
    fn rejects_invalid_attenuation_before_loading() {
        for value in [-1.0, 101.0, f32::NAN, f32::INFINITY] {
            assert!(Denoiser::new(value).is_err());
        }
    }

    #[test]
    fn real_model_framing_silence_and_validation() -> Result<()> {
        let mut denoiser = Denoiser::new(24.0)?;
        assert_eq!(denoiser.frame_samples(), 480);
        assert_eq!(denoiser.delay_samples(), 1440);
        let mut out = [0.0; 480];
        assert!(denoiser.process_frame(&[0.0; 479], &mut out).is_err());
        assert!(denoiser.process_frame(&[f32::NAN; 480], &mut out).is_err());
        assert!(denoiser.process_frame(&[1.1; 480], &mut out).is_err());
        for _ in 0..8 {
            denoiser.process_frame(&[0.0; 480], &mut out)?;
            assert!(out.iter().all(|x| x.abs() < 1e-7));
        }
        Ok(())
    }

    #[test]
    fn dry_mode_preserves_signal_at_documented_delay() -> Result<()> {
        let mut denoiser = Denoiser::new(0.0)?;
        let n = 480 * 12;
        let source: Vec<f32> = (0..n)
            .map(|i| {
                let amplitude = match i / 480 {
                    2..=4 => 0.0,    // A pause must not freeze or compress the time line.
                    5..=7 => 0.0001, // Below upstream's former silence threshold.
                    _ => 0.25,
                };
                amplitude * (i as f32 * 0.07).sin()
            })
            .collect();
        let delay = denoiser.delay_samples();
        let mut padded = source.clone();
        padded.resize(n + delay, 0.0);
        let mut output = vec![0.0; padded.len()];
        for (input, out) in padded.chunks(480).zip(output.chunks_mut(480)) {
            denoiser.process_frame(input, out)?;
        }
        let max_error = source
            .iter()
            .zip(&output[delay..])
            .map(|(a, b)| (a - b).abs())
            .fold(0.0_f32, f32::max);
        assert!(max_error < 1e-6, "dry alignment error {max_error}");
        Ok(())
    }

    #[test]
    fn enhanced_tail_is_emitted_during_zero_padding() -> Result<()> {
        let mut denoiser = Denoiser::new(24.0)?;
        let input: Vec<f32> = (0..480).map(|i| 0.25 * (i as f32 * 0.08).sin()).collect();
        let mut output = [0.0; 480];
        denoiser.process_frame(&input, &mut output)?;
        let mut tail_energy = 0.0;
        for _ in 0..3 {
            denoiser.process_frame(&[0.0; 480], &mut output)?;
            tail_energy += output.iter().map(|x| x * x).sum::<f32>();
        }
        assert!(
            tail_energy > 1e-7,
            "zero padding discarded the delayed signal"
        );
        Ok(())
    }
}
