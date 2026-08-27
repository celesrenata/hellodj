#version 330 core

// Drift: Composite pass fragment shader.
// Draws new visual elements ADDITIVELY on top of the warped+decayed feedback frame:
//   - Oscilloscope waveform (raw audio as glowing distance-field line)
//   - Spectrum ring (FFT bins drawn radially from center)
//   - Beat particle burst (bright dots on transients, seeded by time)
//
// Audio data passed as uniform arrays (no texture lookups needed):
//   u_audio_samples[128] — raw PCM waveform samples (-1..1)
//   u_fft_bands[32]      — FFT magnitude bins (0..1 normalized)
//
// All elements use additive blending (GL_ONE, GL_ONE) — output alpha = glow.

in vec2 v_uv;
out vec4 frag_color;

// --- Audio feature uniforms ---
uniform float u_time;
uniform vec2  u_resolution;
uniform float u_bass;
uniform float u_mids;
uniform float u_highs;
uniform float u_beat;
uniform float u_energy;

// Audio data arrays
uniform float u_audio_samples[128];
uniform float u_fft_bands[32];

// --- Preset-driven composite controls ---
uniform vec3  u_wave_color;
uniform float u_wave_thickness;   // Line thickness in pixels (default 3.0)
uniform float u_ring_radius;      // Normalized radius 0.1-0.5 (default 0.25)
uniform float u_ring_enabled;     // 0.0 or 1.0
uniform float u_particles_enabled; // 0.0 or 1.0

#define PI  3.14159265359
#define TAU 6.28318530718

// --- Utility: pseudo-random hash ---
float hash(float n) {
    return fract(sin(n) * 43758.5453123);
}

float hash2(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
}

// --- Waveform: distance-field glowing oscilloscope line ---
vec3 draw_waveform(vec2 uv, vec2 p) {
    // Map x position to sample index
    float x_norm = uv.x;
    float idx_f = x_norm * 127.0;
    int idx0 = int(floor(idx_f));
    int idx1 = min(idx0 + 1, 127);
    float t = fract(idx_f);

    // Linearly interpolate between adjacent samples for smooth line
    float sample0 = u_audio_samples[idx0];
    float sample1 = u_audio_samples[idx1];
    float wave_val = mix(sample0, sample1, t);

    // Map waveform value to screen Y (centered at 0.5, amplitude ±0.35)
    float wave_y = 0.5 + wave_val * 0.35;

    // Distance from pixel to waveform line
    float dist = abs(uv.y - wave_y);
    float thickness_ndc = u_wave_thickness / u_resolution.y;

    // Glowing line: inverse-distance falloff with steep core
    float glow = thickness_ndc / (dist + thickness_ndc * 0.4);
    glow = pow(glow, 2.8) * 0.35;

    // Animate color with slow hue shift
    vec3 wc = u_wave_color;
    wc = mix(wc, wc.gbr, 0.25 * sin(u_time * 0.4 + x_norm * 2.0));

    // Intensify on beat
    return wc * glow * (1.0 + u_beat * 0.7);
}

// --- Spectrum ring: FFT bins drawn as radial bars from center ---
vec3 draw_spectrum_ring(vec2 p) {
    float radius = length(p);
    float angle = atan(p.y, p.x);
    float norm_angle = (angle + PI) / TAU;  // 0..1

    // Map angle to FFT bin index
    float bin_f = norm_angle * 31.0;
    int bin0 = int(floor(bin_f));
    int bin1 = min(bin0 + 1, 31);
    float t = fract(bin_f);

    // Interpolate FFT magnitude
    float magnitude = mix(u_fft_bands[bin0], u_fft_bands[bin1], t);

    // Ring geometry
    float inner = u_ring_radius + u_beat * 0.03;
    float bar_length = magnitude * 0.45 * (1.0 + u_bass * 0.4);
    float outer = inner + bar_length;

    vec3 ring_color = vec3(0.0);

    // Bar fill
    if (radius > inner && radius < outer) {
        float bar_pos = (radius - inner) / max(bar_length, 0.001);

        // Color gradient: blue at base → magenta at tip
        vec3 col = mix(
            vec3(0.1, 0.4, 1.0),
            vec3(1.0, 0.2, 0.7),
            bar_pos
        );
        col *= 1.0 + u_beat * 0.5;

        // Soft edges
        float edge = smoothstep(inner, inner + 0.008, radius);
        edge *= smoothstep(outer, outer - 0.008, radius);

        ring_color += col * edge * 1.2;
    }

    // Inner ring glow (always visible, subtle)
    float ring_glow = 0.004 / (abs(radius - inner) + 0.004);
    ring_color += vec3(0.15, 0.08, 0.35) * ring_glow * 0.2;

    // Outer tip glow on strong bins
    if (magnitude > 0.3) {
        float tip_dist = abs(radius - outer);
        float tip_glow = 0.003 / (tip_dist + 0.003);
        ring_color += vec3(0.8, 0.3, 1.0) * tip_glow * magnitude * 0.15;
    }

    return ring_color;
}

// --- Beat particles: bright dots that burst outward on transients ---
vec3 draw_particles(vec2 p) {
    vec3 particle_color = vec3(0.0);

    // Only emit when beat exceeds threshold
    if (u_beat < 0.1) return particle_color;

    // Number of particles scales with beat intensity
    int count = int(mix(20.0, 50.0, u_beat));

    for (int i = 0; i < 50; i++) {
        if (i >= count) break;

        float fi = float(i);

        // Deterministic "random" position seeded by particle index + time
        float seed = fi * 7.13 + floor(u_time * 2.0) * 0.37;
        float angle = hash(seed + 0.5) * TAU;
        float speed = hash(seed + 1.3) * 0.7 + 0.3;

        // Particles expand outward from center as beat decays
        float expand = (1.0 - u_beat) * speed * 1.2;
        vec2 particle_pos = vec2(cos(angle), sin(angle)) * expand;

        // Distance from pixel to particle center
        float dist = length(p - particle_pos);
        float size_ndc = 4.0 / u_resolution.y;

        // Point-light falloff
        float bright = size_ndc / (dist + size_ndc * 0.5);
        bright = pow(bright, 3.5) * u_beat;

        // Per-particle hue (rainbow spread, slowly rotating)
        float hue = hash(fi * 1.37) + u_time * 0.08;
        vec3 pc = vec3(
            0.5 + 0.5 * cos(hue * TAU),
            0.5 + 0.5 * cos(hue * TAU + 2.094),
            0.5 + 0.5 * cos(hue * TAU + 4.189)
        );

        particle_color += pc * bright * 0.5;
    }

    return particle_color;
}

void main() {
    vec2 uv = v_uv;

    // Aspect-corrected coordinates centered at origin
    vec2 p = uv * 2.0 - 1.0;
    p.x *= u_resolution.x / u_resolution.y;

    vec3 composite = vec3(0.0);

    // --- Oscilloscope waveform (always enabled if data present) ---
    composite += draw_waveform(uv, p);

    // --- Spectrum ring ---
    if (u_ring_enabled > 0.5) {
        composite += draw_spectrum_ring(p);
    }

    // --- Beat particles ---
    if (u_particles_enabled > 0.5) {
        composite += draw_particles(p);
    }

    // --- Center flash on hard beat ---
    float center_dist = length(p);
    float flash = exp(-center_dist * 3.5) * u_beat * u_beat * 0.35;
    composite += vec3(0.5, 0.35, 0.9) * flash;

    // Output: RGB = additive glow color, A = intensity for additive blend
    frag_color = vec4(composite, 1.0);
}
