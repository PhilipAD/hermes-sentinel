// Build script for sentinel-audio.
//
// We don't link to anything exotic — cpal pulls its own platform deps in via
// its build script. This file exists so cargo treats the crate as a regular
// binary build and to provide one place to add platform shims if Sentinel
// later needs to bundle a copy of BlackHole/PulseAudio config or similar.

fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed=src/main.rs");
}
