use std::fs::{self, OpenOptions};
use std::io::{self, BufWriter, Read, Write};
use std::path::{Path, PathBuf};
use std::time::Instant;

use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};
use sao_noise::{StreamDenoiser, SAMPLE_RATE, TELEPHONY_SAMPLE_RATE};
use serde_json::{json, Value};

#[derive(Parser)]
#[command(
    name = "sao-denoise",
    about = "Streaming DeepFilterNet3 speech noise suppression"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Exchange fixed-size mono PCM16 little-endian frames on stdin/stdout.
    /// Stdout starts with one JSON configuration line, followed by raw PCM.
    Stream {
        #[arg(long, default_value = "24", value_parser = parse_attenuation)]
        atten_lim_db: f32,
        #[arg(long, default_value = "48000", value_parser = parse_sample_rate)]
        sample_rate: u32,
    },
    /// Denoise a mono PCM16 8 or 48 kHz WAV, preserving rate, length and alignment.
    /// Existing output files are never overwritten.
    Wav {
        input: PathBuf,
        output: PathBuf,
        #[arg(long, default_value = "24", value_parser = parse_attenuation)]
        atten_lim_db: f32,
    },
    /// Measure inference throughput with deterministic synthetic audio.
    /// This does not measure speech quality.
    Benchmark {
        #[arg(long, default_value = "10", value_parser = parse_seconds)]
        seconds: f64,
        #[arg(long, default_value = "24", value_parser = parse_attenuation)]
        atten_lim_db: f32,
        #[arg(long, default_value = "48000", value_parser = parse_sample_rate)]
        sample_rate: u32,
    },
}

fn parse_sample_rate(value: &str) -> std::result::Result<u32, String> {
    let rate = value
        .parse()
        .map_err(|_| "expected a sample rate".to_owned())?;
    if !matches!(rate, TELEPHONY_SAMPLE_RATE | SAMPLE_RATE) {
        return Err("supported sample rates are 8000 and 48000 Hz".into());
    }
    Ok(rate)
}

fn parse_attenuation(value: &str) -> std::result::Result<f32, String> {
    let value: f32 = value.parse().map_err(|_| "expected a number".to_owned())?;
    if !value.is_finite() || !(0.0..=100.0).contains(&value) {
        return Err("attenuation must be finite and between 0 and 100 dB".into());
    }
    Ok(value)
}

fn parse_seconds(value: &str) -> std::result::Result<f64, String> {
    let value: f64 = value.parse().map_err(|_| "expected a number".to_owned())?;
    if !value.is_finite() || value <= 0.0 || value > 3600.0 {
        return Err("duration must be finite, greater than zero, and at most 3600 seconds".into());
    }
    Ok(value)
}

fn main() -> Result<()> {
    match Cli::parse().command {
        Command::Stream {
            atten_lim_db,
            sample_rate,
        } => {
            let mut denoiser = StreamDenoiser::new(sample_rate, atten_lim_db)?;
            let stdin = io::stdin();
            let stdout = io::stdout();
            stream(&mut stdin.lock(), &mut stdout.lock(), &mut denoiser)
        }
        Command::Wav {
            input,
            output,
            atten_lim_db,
        } => wav(&input, &output, atten_lim_db),
        Command::Benchmark {
            seconds,
            atten_lim_db,
            sample_rate,
        } => benchmark(seconds, atten_lim_db, sample_rate),
    }
}

fn stream<R: Read, W: Write>(
    reader: &mut R,
    writer: &mut W,
    denoiser: &mut StreamDenoiser,
) -> Result<()> {
    let frame_samples = denoiser.frame_samples();
    let mut header = json!({
        "protocol": 1,
        "sample_rate": denoiser.sample_rate(),
        "frame_samples": frame_samples,
        "algorithmic_delay_samples": denoiser.delay_samples(),
        "model": "DeepFilterNet3",
    });
    if denoiser.sample_rate() != SAMPLE_RATE {
        header["model_sample_rate"] = json!(SAMPLE_RATE);
    }
    writeln!(writer, "{header}")?;
    writer.flush()?;

    let mut bytes = vec![0_u8; frame_samples * 2];
    let mut input = vec![0.0; frame_samples];
    let mut output = vec![0.0; frame_samples];
    while read_complete_frame(reader, &mut bytes)? {
        decode_pcm16(&bytes, &mut input)?;
        denoiser.process_frame(&input, &mut output)?;
        encode_pcm16(&output, &mut bytes)?;
        writer.write_all(&bytes)?;
        writer.flush()?;
    }
    Ok(())
}

/// Pipes can return fewer bytes than requested without reaching EOF.
/// A partial final frame is an error: streaming never invents missing audio.
fn read_complete_frame<R: Read>(reader: &mut R, buffer: &mut [u8]) -> Result<bool> {
    if buffer.is_empty() {
        bail!("PCM frame buffer cannot be empty");
    }
    let mut filled = 0;
    while filled < buffer.len() {
        match reader.read(&mut buffer[filled..]) {
            Ok(0) if filled == 0 => return Ok(false),
            Ok(0) => bail!(
                "partial PCM frame at EOF: received {filled} bytes, expected {} bytes; supply complete frames",
                buffer.len()
            ),
            Ok(count) => filled += count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error).context("reading PCM frame"),
        }
    }
    Ok(true)
}

fn decode_pcm16(bytes: &[u8], output: &mut [f32]) -> Result<()> {
    if bytes.len() != output.len() * 2 {
        bail!("PCM input byte count does not match sample count");
    }
    for (pair, sample) in bytes.chunks_exact(2).zip(output) {
        *sample = i16::from_le_bytes([pair[0], pair[1]]) as f32 / 32768.0;
    }
    Ok(())
}

fn pcm16(sample: f32) -> Result<i16> {
    if !sample.is_finite() {
        bail!("denoiser returned a non-finite audio sample");
    }
    Ok((sample.clamp(-1.0, 1.0) * 32768.0)
        .round()
        .clamp(-32768.0, 32767.0) as i16)
}

fn encode_pcm16(samples: &[f32], output: &mut [u8]) -> Result<()> {
    if output.len() != samples.len() * 2 {
        bail!("PCM output byte count does not match sample count");
    }
    for (&sample, pair) in samples.iter().zip(output.chunks_exact_mut(2)) {
        pair.copy_from_slice(&pcm16(sample)?.to_le_bytes());
    }
    Ok(())
}

fn validate_wav_spec(spec: hound::WavSpec) -> Result<()> {
    if spec.channels != 1
        || !matches!(spec.sample_rate, TELEPHONY_SAMPLE_RATE | SAMPLE_RATE)
        || spec.sample_format != hound::SampleFormat::Int
        || spec.bits_per_sample != 16
    {
        bail!(
            "expected mono PCM16 WAV at 8000 or 48000 Hz; got {} channel(s), {} Hz, {:?}, {} bits",
            spec.channels,
            spec.sample_rate,
            spec.sample_format,
            spec.bits_per_sample
        );
    }
    Ok(())
}

fn wav(input_path: &Path, output_path: &Path, atten_lim_db: f32) -> Result<()> {
    let input_identity = fs::canonicalize(input_path)
        .with_context(|| format!("opening input {}", input_path.display()))?;
    if let Ok(output_identity) = fs::canonicalize(output_path) {
        if input_identity == output_identity {
            bail!("input and output must be different files; in-place processing is unsupported");
        }
        bail!("output already exists: {}", output_path.display());
    }

    let mut reader = hound::WavReader::open(input_path).context("reading WAV header")?;
    let spec = reader.spec();
    validate_wav_spec(spec)?;
    let samples: Vec<i16> = reader
        .samples::<i16>()
        .collect::<std::result::Result<_, _>>()
        .context("reading PCM16 WAV samples")?;
    let mut denoiser = StreamDenoiser::new(spec.sample_rate, atten_lim_db)?;
    let frame_samples = denoiser.frame_samples();
    let delay_samples = denoiser.delay_samples();
    let output_end = samples
        .len()
        .checked_add(delay_samples)
        .context("WAV is too large")?;
    let frame_count = if samples.is_empty() {
        0
    } else {
        output_end.div_ceil(frame_samples)
    };
    let mut input = vec![0.0; frame_samples];
    let mut output = vec![0.0; frame_samples];
    let mut processed = Vec::with_capacity(samples.len());
    let mut frame_seconds = Vec::with_capacity(frame_count);
    for frame_index in 0..frame_count {
        let start = frame_index * frame_samples;
        input.fill(0.0);
        if start < samples.len() {
            let count = frame_samples.min(samples.len() - start);
            for (destination, &source) in input[..count]
                .iter_mut()
                .zip(&samples[start..start + count])
            {
                *destination = source as f32 / 32768.0;
            }
        }
        let timer = Instant::now();
        denoiser.process_frame(&input, &mut output)?;
        frame_seconds.push(timer.elapsed().as_secs_f64());
        // Validate all samples, including the startup and drain regions we discard.
        for (offset, &sample) in output.iter().enumerate() {
            let sample = pcm16(sample)?;
            let position = start + offset;
            if position >= delay_samples && position < output_end {
                processed.push(sample);
            }
        }
    }
    if processed.len() != samples.len() {
        bail!("internal error: delay compensation changed the output length");
    }

    // create_new also rejects files created while inference was running.
    let file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(output_path)
        .with_context(|| {
            format!(
                "creating new output {} (existing files are never overwritten)",
                output_path.display()
            )
        })?;
    let mut writer = hound::WavWriter::new(BufWriter::new(file), spec)?;
    for sample in processed {
        writer.write_sample(sample)?;
    }
    writer.finalize().context("finalizing output WAV")?;
    let mut report = timing_report(
        &frame_seconds,
        samples.len(),
        frame_samples,
        delay_samples,
        spec.sample_rate,
    );
    report["operation"] = json!("wav");
    report["atten_lim_db"] = json!(atten_lim_db);
    report["input_samples"] = json!(samples.len());
    report["output_samples"] = json!(samples.len());
    report["delay_compensated"] = json!(true);
    eprintln!("{report}");
    Ok(())
}

fn benchmark(seconds: f64, atten_lim_db: f32, sample_rate: u32) -> Result<()> {
    let mut denoiser = StreamDenoiser::new(sample_rate, atten_lim_db)?;
    let frame_samples = denoiser.frame_samples();
    let frames = (seconds * sample_rate as f64 / frame_samples as f64).ceil() as usize;
    let mut input = vec![0.0; frame_samples];
    let mut output = vec![0.0; frame_samples];
    let mut frame_seconds = Vec::with_capacity(frames);
    let mut noise_state = 0x1234_5678_u32;
    for frame in 0..frames {
        for (offset, sample) in input.iter_mut().enumerate() {
            let time = (frame * frame_samples + offset) as f64 / sample_rate as f64;
            noise_state = noise_state
                .wrapping_mul(1_664_525)
                .wrapping_add(1_013_904_223);
            let noise = noise_state as f64 / u32::MAX as f64 * 2.0 - 1.0;
            let phase = std::f64::consts::TAU * time;
            let envelope = 0.45 + 0.35 * (phase * 3.2).sin();
            *sample = (envelope
                * (0.22 * (phase * 137.0).sin()
                    + 0.12 * (phase * 274.0).sin()
                    + 0.06 * (phase * 548.0).sin())
                + noise * 0.035) as f32;
        }
        let timer = Instant::now();
        denoiser.process_frame(&input, &mut output)?;
        frame_seconds.push(timer.elapsed().as_secs_f64());
        if output.iter().any(|sample| !sample.is_finite()) {
            bail!("denoiser returned a non-finite audio sample");
        }
    }
    let mut report = timing_report(
        &frame_seconds,
        frames * frame_samples,
        frame_samples,
        denoiser.delay_samples(),
        sample_rate,
    );
    report["operation"] = json!("benchmark");
    report["fixture"] = json!("deterministic_synthetic_multitone_and_noise");
    report["measures_speech_quality"] = json!(false);
    report["requested_audio_seconds"] = json!(seconds);
    report["atten_lim_db"] = json!(atten_lim_db);
    report["warmup_frames"] = json!(0);
    println!("{report}");
    Ok(())
}

fn timing_report(
    frame_seconds: &[f64],
    audio_samples: usize,
    frame_samples: usize,
    delay_samples: usize,
    sample_rate: u32,
) -> Value {
    let processing_seconds: f64 = frame_seconds.iter().sum();
    let audio_seconds = audio_samples as f64 / sample_rate as f64;
    let mut sorted = frame_seconds.to_vec();
    sorted.sort_by(f64::total_cmp);
    let percentile_ms = |percentile: f64| -> Option<f64> {
        if sorted.is_empty() {
            None
        } else {
            Some(sorted[((sorted.len() - 1) as f64 * percentile).round() as usize] * 1000.0)
        }
    };
    let mut report = json!({
        "model": "DeepFilterNet3",
        "sample_rate": sample_rate,
        "frame_samples": frame_samples,
        "algorithmic_delay_samples": delay_samples,
        "frames_processed": frame_seconds.len(),
        "processing_compute_seconds": processing_seconds,
        "audio_seconds": audio_seconds,
        "real_time_factor": if audio_seconds > 0.0 { Some(processing_seconds / audio_seconds) } else { None },
        "frame_p50_ms": percentile_ms(0.50),
        "frame_p95_ms": percentile_ms(0.95),
        "timing_scope": "process_frame_only; excludes model loading, I/O, PCM conversion and fixture generation",
    });
    if sample_rate != SAMPLE_RATE {
        report["model_sample_rate"] = json!(SAMPLE_RATE);
        report["timing_scope"] = json!("process_frame_including_resampling; excludes model loading, I/O, PCM conversion and fixture generation");
    }
    report
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    struct FragmentedReader {
        inner: Cursor<Vec<u8>>,
        next_interrupted: bool,
    }

    impl Read for FragmentedReader {
        fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
            if self.next_interrupted {
                self.next_interrupted = false;
                return Err(io::Error::from(io::ErrorKind::Interrupted));
            }
            self.next_interrupted = true;
            let count = buffer.len().min(3);
            self.inner.read(&mut buffer[..count])
        }
    }

    #[test]
    fn fragmented_and_interrupted_reads_preserve_frame_boundaries() {
        let mut reader = FragmentedReader {
            inner: Cursor::new((0..16).collect()),
            next_interrupted: true,
        };
        let mut buffer = [0; 8];
        assert!(read_complete_frame(&mut reader, &mut buffer).unwrap());
        assert_eq!(buffer, [0, 1, 2, 3, 4, 5, 6, 7]);
        assert!(read_complete_frame(&mut reader, &mut buffer).unwrap());
        assert_eq!(buffer, [8, 9, 10, 11, 12, 13, 14, 15]);
        assert!(!read_complete_frame(&mut reader, &mut buffer).unwrap());
    }

    #[test]
    fn partial_final_frame_is_rejected_without_padding() {
        let mut buffer = [99; 8];
        let error = read_complete_frame(&mut Cursor::new([1, 2, 3]), &mut buffer).unwrap_err();
        assert!(error.to_string().contains("received 3 bytes, expected 8"));
        assert_eq!(buffer, [1, 2, 3, 99, 99, 99, 99, 99]);
    }

    #[test]
    fn all_pcm16_values_round_trip_exactly() {
        let samples: Vec<i16> = (i16::MIN..=i16::MAX).collect();
        let bytes: Vec<u8> = samples
            .iter()
            .flat_map(|sample| sample.to_le_bytes())
            .collect();
        let mut floats = vec![0.0; samples.len()];
        decode_pcm16(&bytes, &mut floats).unwrap();
        let mut encoded = vec![0; bytes.len()];
        encode_pcm16(&floats, &mut encoded).unwrap();
        assert_eq!(encoded, bytes);
    }

    #[test]
    fn pcm16_clips_finite_values_and_rejects_nonfinite_values() {
        assert_eq!(pcm16(-1.5).unwrap(), i16::MIN);
        assert_eq!(pcm16(1.5).unwrap(), i16::MAX);
        assert_eq!(pcm16(0.0).unwrap(), 0);
        assert_eq!(pcm16(0.5).unwrap(), 16384);
        assert!(pcm16(f32::NAN).is_err());
        assert!(pcm16(f32::INFINITY).is_err());
        assert!(pcm16(f32::NEG_INFINITY).is_err());
    }

    #[test]
    fn wav_format_validation_rejects_unsupported_audio() {
        let spec = hound::WavSpec {
            channels: 1,
            sample_rate: SAMPLE_RATE,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        };
        assert!(validate_wav_spec(spec).is_ok());
        assert!(validate_wav_spec(hound::WavSpec {
            sample_rate: TELEPHONY_SAMPLE_RATE,
            ..spec
        })
        .is_ok());
        assert!(validate_wav_spec(hound::WavSpec {
            channels: 2,
            ..spec
        })
        .is_err());
        assert!(validate_wav_spec(hound::WavSpec {
            sample_rate: 16000,
            ..spec
        })
        .is_err());
        assert!(validate_wav_spec(hound::WavSpec {
            bits_per_sample: 24,
            ..spec
        })
        .is_err());
        assert!(validate_wav_spec(hound::WavSpec {
            sample_format: hound::SampleFormat::Float,
            ..spec
        })
        .is_err());
    }

    #[test]
    fn stream_rate_parser_accepts_telephone_and_full_band_only() {
        assert_eq!(parse_sample_rate("8000").unwrap(), 8000);
        assert_eq!(parse_sample_rate("48000").unwrap(), 48000);
        for invalid in ["8", "16000", "0", "NaN", "-8000", "8khz"] {
            assert!(parse_sample_rate(invalid).is_err());
        }
    }
}
