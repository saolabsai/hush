use std::f64::consts::PI;

pub(crate) const RATIO: usize = 6;
// Each symmetric 3.75 kHz Blackman FIR adds 192 samples at the model rate.
pub(crate) const FILTER_TAPS: usize = 385;
pub(crate) const CONVERSION_DELAY_SAMPLES: usize = (FILTER_TAPS - 1) / RATIO;
const INPUT_HISTORY: usize = FILTER_TAPS.div_ceil(RATIO);

pub(crate) struct TelephoneResampler {
    coefficients: [f64; FILTER_TAPS],
    input: [f32; INPUT_HISTORY],
    input_head: usize,
    output: [f32; FILTER_TAPS],
    output_head: usize,
    phase: usize,
}

impl TelephoneResampler {
    pub(crate) fn new() -> Self {
        let mut coefficients = [0.0; FILTER_TAPS];
        let center = (FILTER_TAPS - 1) as f64 / 2.0;
        let cutoff = 3_750.0 / 48_000.0;
        for (index, coefficient) in coefficients.iter_mut().enumerate() {
            let offset = index as f64 - center;
            let sinc = if offset == 0.0 {
                2.0 * cutoff
            } else {
                (2.0 * PI * cutoff * offset).sin() / (PI * offset)
            };
            let phase = 2.0 * PI * index as f64 / (FILTER_TAPS - 1) as f64;
            let window = 0.42 - 0.5 * phase.cos() + 0.08 * (2.0 * phase).cos();
            *coefficient = sinc * window;
        }
        let gain: f64 = coefficients.iter().sum();
        for coefficient in &mut coefficients {
            *coefficient /= gain;
        }
        Self {
            coefficients,
            input: [0.0; INPUT_HISTORY],
            input_head: 0,
            output: [0.0; FILTER_TAPS],
            output_head: 0,
            phase: 0,
        }
    }

    pub(crate) fn upsample(&mut self, input: &[f32], output: &mut [f32]) {
        assert_eq!(output.len(), input.len() * RATIO);
        for (&sample, phases) in input.iter().zip(output.chunks_exact_mut(RATIO)) {
            self.input[self.input_head] = sample;
            for (phase, destination) in phases.iter_mut().enumerate() {
                let mut value = 0.0;
                for (lag, &coefficient) in
                    self.coefficients[phase..].iter().step_by(RATIO).enumerate()
                {
                    let index = (self.input_head + INPUT_HISTORY - lag) % INPUT_HISTORY;
                    value += self.input[index] as f64 * coefficient;
                }
                *destination = (value * RATIO as f64) as f32;
            }
            self.input_head = (self.input_head + 1) % INPUT_HISTORY;
        }
    }

    pub(crate) fn downsample(&mut self, input: &[f32], output: &mut [f32]) {
        assert_eq!(input.len(), output.len() * RATIO);
        let mut written = 0;
        for &sample in input {
            self.output[self.output_head] = sample;
            if self.phase == 0 {
                let mut value = 0.0;
                for (lag, &coefficient) in self.coefficients.iter().enumerate() {
                    let index = (self.output_head + FILTER_TAPS - lag) % FILTER_TAPS;
                    value += self.output[index] as f64 * coefficient;
                }
                output[written] = value as f32;
                written += 1;
            }
            self.output_head = (self.output_head + 1) % FILTER_TAPS;
            self.phase = (self.phase + 1) % RATIO;
        }
        assert_eq!(written, output.len());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tone(frequency: f64, rate: f64, length: usize) -> Vec<f32> {
        (0..length)
            .map(|i| (0.25 * (2.0 * PI * frequency * i as f64 / rate).sin()) as f32)
            .collect()
    }

    fn amplitude(samples: &[f32], frequency: f64, rate: f64) -> f64 {
        let (real, imaginary) = samples.iter().enumerate().fold((0.0, 0.0), |sum, (i, &x)| {
            let phase = 2.0 * PI * frequency * i as f64 / rate;
            (
                sum.0 + x as f64 * phase.cos(),
                sum.1 + x as f64 * phase.sin(),
            )
        });
        2.0 * real.hypot(imaginary) / samples.len() as f64
    }

    #[test]
    fn dc_gain_and_telephone_speech_passband_are_preserved() {
        let mut resampler = TelephoneResampler::new();
        let mut up = vec![0.0; 8_000 * RATIO];
        let mut down = vec![0.0; 8_000];
        resampler.upsample(&vec![0.25; 8_000], &mut up);
        resampler.downsample(&up, &mut down);
        assert!(up[600..].iter().all(|x| (x - 0.25).abs() < 1e-6));
        assert!(down[200..].iter().all(|x| (x - 0.25).abs() < 1e-6));
        for frequency in [300.0, 1_000.0, 3_000.0, 3_400.0] {
            let mut resampler = TelephoneResampler::new();
            resampler.upsample(&tone(frequency, 8_000.0, 8_000), &mut up);
            resampler.downsample(&up, &mut down);
            let gain = amplitude(&down[800..], frequency, 8_000.0) / 0.25;
            assert!((gain - 1.0).abs() < 0.002, "{frequency} Hz gain {gain}");
        }
    }

    #[test]
    fn interpolation_images_and_decimation_aliases_are_rejected() {
        let mut up = vec![0.0; 48_000];
        let mut resampler = TelephoneResampler::new();
        resampler.upsample(&tone(3_400.0, 8_000.0, 8_000), &mut up);
        let passband = amplitude(&up[4_800..], 3_400.0, 48_000.0);
        let image = amplitude(&up[4_800..], 4_600.0, 48_000.0);
        assert!(passband > 0.249, "interpolation lost speech energy");
        assert!(
            image / passband < 0.0002,
            "image ratio {}",
            image / passband
        );

        for frequency in [4_600.0, 6_000.0, 10_000.0, 15_000.0, 20_000.0] {
            let mut resampler = TelephoneResampler::new();
            let mut down = vec![0.0; 8_000];
            resampler.downsample(&tone(frequency, 48_000.0, 48_000), &mut down);
            let rms =
                (down[800..].iter().map(|x| (*x as f64).powi(2)).sum::<f64>() / 7_200.0).sqrt();
            assert!(rms < 0.00004, "{frequency} Hz aliased with RMS {rms}");
        }
    }

    #[test]
    fn impulse_alignment_chunking_and_fresh_stream_state_agree() {
        let mut source = vec![0.0; 800];
        source[100] = 0.25;
        let mut whole = TelephoneResampler::new();
        let mut up = vec![0.0; source.len() * RATIO];
        let mut expected = vec![0.0; source.len()];
        whole.upsample(&source, &mut up);
        whole.downsample(&up, &mut expected);
        let peak = expected
            .iter()
            .enumerate()
            .max_by(|a, b| a.1.abs().total_cmp(&b.1.abs()))
            .unwrap()
            .0;
        assert_eq!(peak, 100 + CONVERSION_DELAY_SAMPLES);

        let mut split = TelephoneResampler::new();
        let mut actual = vec![0.0; source.len()];
        for (input, output) in source.chunks(80).zip(actual.chunks_mut(80)) {
            let mut intermediate = [0.0; 480];
            split.upsample(input, &mut intermediate);
            split.downsample(&intermediate, output);
        }
        assert_eq!(actual, expected);
        let mut fresh = TelephoneResampler::new();
        let mut silent_up = [1.0; 480];
        let mut silent_down = [1.0; 80];
        fresh.upsample(&[0.0; 80], &mut silent_up);
        fresh.downsample(&silent_up, &mut silent_down);
        assert!(silent_up.iter().chain(&silent_down).all(|&x| x == 0.0));
    }
}
