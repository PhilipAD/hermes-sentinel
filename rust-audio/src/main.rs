// sentinel-audio — Hermes Sentinel native audio engine.
//
// Captures dual-channel PCM (microphone + system loopback) via cpal, applies a
// simple energy-based VAD (when --vad is set), and emits framed PCM16 chunks
// on stdout in the wire format documented in the Python `audio_capture` module:
//
//   u32 le  chunk_id
//   u64 le  timestamp_ms
//   u8      channel_tag (0=mic, 1=system, 2=mixed)
//   u32 le  payload_len
//   bytes   payload (PCM16 little-endian mono)
//
// The Python side reads this verbatim with `struct.unpack("<IQBI", header)`.
//
// This is a scaffold. Cross-platform system-audio loopback has OS-specific
// quirks (Linux: monitor source on PulseAudio; Windows: WASAPI loopback;
// macOS: BlackHole aggregate device). The scaffold covers the wire format and
// graceful fallback to mic-only — extend per-platform under target_os blocks
// when wiring real system loopback for production.

use anyhow::{anyhow, Result};
use byteorder::{LittleEndian, WriteBytesExt};
use clap::Parser;
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{Sample, SampleFormat, StreamConfig};
use std::io::{stdout, Write};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

/// CLI args. Defaults match the Python `audio_capture.open_capture()` config.
#[derive(Parser, Debug)]
#[command(name = "sentinel-audio", version, about = "Hermes Sentinel audio engine")]
struct Args {
    /// Channel count. 1 = mic only, 2 = mic + system loopback.
    #[arg(long, default_value_t = 2)]
    channels: u8,

    /// Sample rate in Hz.
    #[arg(long, default_value_t = 16_000)]
    sample_rate: u32,

    /// Frame size in milliseconds.
    #[arg(long, default_value_t = 20)]
    chunk_ms: u32,

    /// Enable energy-based VAD gating.
    #[arg(long, default_value_t = false)]
    vad: bool,

    /// Override the default input device by name.
    #[arg(long)]
    input_device: Option<String>,

    /// Override the system loopback device by name (overrides auto-detect).
    #[arg(long)]
    loopback_device: Option<String>,
}

/// Channel tags as expected by the Python wire format.
const CH_MIC: u8 = 0;
const CH_SYSTEM: u8 = 1;
const CH_MIXED: u8 = 2;

fn main() -> Result<()> {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();
    let args = Args::parse();
    log::info!(
        "sentinel-audio starting: channels={} sample_rate={} chunk_ms={} vad={}",
        args.channels, args.sample_rate, args.chunk_ms, args.vad
    );

    let host = cpal::default_host();
    let mic = pick_input(&host, args.input_device.as_deref())?;
    log::info!("mic device: {}", mic.name().unwrap_or_else(|_| "?".into()));

    let chunk_id = Arc::new(AtomicU32::new(0));
    let frames_per_chunk = (args.sample_rate as f32 * (args.chunk_ms as f32 / 1000.0)) as usize;

    // Build the mic stream.
    let mic_stream = build_input_stream(
        &mic,
        args.sample_rate,
        frames_per_chunk,
        args.vad,
        CH_MIC,
        chunk_id.clone(),
    )?;
    mic_stream.play()?;

    // System loopback — best effort, only when channels=2.
    let _system_stream = if args.channels >= 2 {
        match pick_loopback(&host, args.loopback_device.as_deref()) {
            Ok(dev) => {
                log::info!(
                    "system loopback device: {}",
                    dev.name().unwrap_or_else(|_| "?".into())
                );
                let s = build_input_stream(
                    &dev,
                    args.sample_rate,
                    frames_per_chunk,
                    args.vad,
                    CH_SYSTEM,
                    chunk_id.clone(),
                )?;
                s.play()?;
                Some(s)
            }
            Err(e) => {
                log::warn!(
                    "no system loopback available ({}). Falling back to mic-only.",
                    e
                );
                None
            }
        }
    } else {
        None
    };

    // Block forever until killed.
    log::info!("sentinel-audio running — emitting PCM16 framed chunks on stdout");
    loop {
        std::thread::park();
    }
}

fn pick_input(
    host: &cpal::Host,
    name: Option<&str>,
) -> Result<cpal::Device> {
    if let Some(want) = name {
        for d in host.input_devices()? {
            if d.name().map(|n| n.eq_ignore_ascii_case(want)).unwrap_or(false) {
                return Ok(d);
            }
        }
    }
    host.default_input_device()
        .ok_or_else(|| anyhow!("no default input device"))
}

fn pick_loopback(
    host: &cpal::Host,
    name: Option<&str>,
) -> Result<cpal::Device> {
    // Linux: PulseAudio exposes monitor sources as input devices ending in
    // ".monitor". macOS: requires a BlackHole virtual device. Windows:
    // WASAPI loopback selects via the default *output* device — that case
    // requires extra work in cpal that's out of scope for this scaffold.
    if let Some(want) = name {
        for d in host.input_devices()? {
            if d.name().map(|n| n.eq_ignore_ascii_case(want)).unwrap_or(false) {
                return Ok(d);
            }
        }
    }
    for d in host.input_devices()? {
        let n = d.name().unwrap_or_default().to_lowercase();
        if n.contains(".monitor") || n.contains("blackhole") || n.contains("loopback") {
            return Ok(d);
        }
    }
    Err(anyhow!("no monitor / loopback input device discovered"))
}

fn build_input_stream(
    device: &cpal::Device,
    sample_rate: u32,
    frames_per_chunk: usize,
    vad: bool,
    channel_tag: u8,
    chunk_id: Arc<AtomicU32>,
) -> Result<cpal::Stream> {
    let supported = device.default_input_config()?;
    let sample_format = supported.sample_format();
    let config = StreamConfig {
        channels: supported.channels(),
        sample_rate: cpal::SampleRate(sample_rate),
        buffer_size: cpal::BufferSize::Default,
    };
    let in_channels = config.channels as usize;
    let mut accum: Vec<i16> = Vec::with_capacity(frames_per_chunk);

    let err_fn = |e| log::error!("audio stream error: {}", e);

    let stream = match sample_format {
        SampleFormat::F32 => device.build_input_stream(
            &config,
            move |data: &[f32], _| {
                ingest_f32(
                    data,
                    in_channels,
                    &mut accum,
                    frames_per_chunk,
                    vad,
                    channel_tag,
                    &chunk_id,
                );
            },
            err_fn,
            None,
        )?,
        SampleFormat::I16 => device.build_input_stream(
            &config,
            move |data: &[i16], _| {
                ingest_i16(
                    data,
                    in_channels,
                    &mut accum,
                    frames_per_chunk,
                    vad,
                    channel_tag,
                    &chunk_id,
                );
            },
            err_fn,
            None,
        )?,
        SampleFormat::U16 => device.build_input_stream(
            &config,
            move |data: &[u16], _| {
                let conv: Vec<i16> = data.iter().map(|s| s.to_sample::<i16>()).collect();
                ingest_i16(
                    &conv,
                    in_channels,
                    &mut accum,
                    frames_per_chunk,
                    vad,
                    channel_tag,
                    &chunk_id,
                );
            },
            err_fn,
            None,
        )?,
        other => return Err(anyhow!("unsupported sample format: {:?}", other)),
    };
    Ok(stream)
}

fn ingest_f32(
    data: &[f32],
    in_channels: usize,
    accum: &mut Vec<i16>,
    frames_per_chunk: usize,
    vad: bool,
    channel_tag: u8,
    chunk_id: &AtomicU32,
) {
    for frame in data.chunks(in_channels) {
        // Downmix to mono.
        let mono: f32 = frame.iter().copied().sum::<f32>() / in_channels as f32;
        let s = (mono.clamp(-1.0, 1.0) * 32767.0) as i16;
        accum.push(s);
        if accum.len() >= frames_per_chunk {
            emit(chunk_id, channel_tag, accum, vad);
            accum.clear();
        }
    }
}

fn ingest_i16(
    data: &[i16],
    in_channels: usize,
    accum: &mut Vec<i16>,
    frames_per_chunk: usize,
    vad: bool,
    channel_tag: u8,
    chunk_id: &AtomicU32,
) {
    for frame in data.chunks(in_channels) {
        let mut sum: i32 = 0;
        for &s in frame {
            sum += s as i32;
        }
        let s = (sum / in_channels as i32).clamp(i16::MIN as i32, i16::MAX as i32) as i16;
        accum.push(s);
        if accum.len() >= frames_per_chunk {
            emit(chunk_id, channel_tag, accum, vad);
            accum.clear();
        }
    }
}

fn emit(chunk_id: &AtomicU32, channel_tag: u8, samples: &[i16], vad: bool) {
    if vad && !is_voiced(samples) {
        // Drop silent chunks under VAD; keeps the WS upstream small.
        return;
    }
    let id = chunk_id.fetch_add(1, Ordering::SeqCst);
    let ts_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0);
    let payload_bytes: Vec<u8> = samples
        .iter()
        .flat_map(|s| s.to_le_bytes().to_vec())
        .collect();
    let payload_len = payload_bytes.len() as u32;
    let mut out = stdout().lock();
    if out.write_u32::<LittleEndian>(id).is_err() { return; }
    if out.write_u64::<LittleEndian>(ts_ms).is_err() { return; }
    if out.write_u8(channel_tag).is_err() { return; }
    if out.write_u32::<LittleEndian>(payload_len).is_err() { return; }
    let _ = out.write_all(&payload_bytes);
    let _ = out.flush();
}

/// Cheap energy gate — RMS above threshold counts as voiced.
fn is_voiced(samples: &[i16]) -> bool {
    if samples.is_empty() {
        return false;
    }
    let sumsq: f64 = samples
        .iter()
        .map(|&s| {
            let f = s as f64 / 32768.0;
            f * f
        })
        .sum();
    let rms = (sumsq / samples.len() as f64).sqrt();
    rms > 0.005
}
