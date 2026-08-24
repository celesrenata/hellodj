#version 330 core

// Drift: Composite pass fragment shader.
// Draws new visual elements on top of the warped feedback frame:
// - Audio waveform (distance-field glowing line)
// - Spectrum ring (radial frequency bars)
// - Beat flash
// All composited with additive blending.

in vec2 v_uv;
out vec4 frag_color;

uniform float u_time;
uniform vec2  u_resolution;
uniform float u_beat;
uniform float u_bass;
uniform float u_mids;
uniform float u_highs;
uniform float u_bpm;

// FFT data (64 bins packed into a 1D texture)
uniform sampler1D u_fft;

// Waveform data (512 samples packed into a 1D texture)
uniform sampler1D u_waveform;

// Composite controls (from preset)
uniform float u_wave_enabled;    // 0 or 1
uniform float u_wave_thickness;  // Line thickness in pixels
uniform vec3  u_wave_color;
uniform float u_ring_enabled;    // 0 or 1
uniform float u_ring_radius;     // 0.1-0.5
uniform float u_ring_glow;
uniform float u_particles_enabled;  // 0 or 1
uniform float u_particle_count;
uniform float u_particle_size;

#define PI 3.14159265359
#define TAU 6.28318530718

// Pseudo-random
float hash(float n) { return fract(sin(n) * 43758.5453123); }

void main() {
    vec2 uv = v_uv;
    vec2 p = uv * 2.0 - 1.0;
    p.x *= u_resolution.x / u_resolution.y;

    vec3 composite = vec3(0.0);

    // --- Waveform oscilloscope ---
    if (u_wave_enabled > 0.5) {
        // Sample waveform at this x position
        float wave_sample = texture(u_waveform, uv.x).r;
        // Map to [-0.4, 0.4] centered at y=0.5
        float wave_y = 0.5 + wave_sample * 0.35;

        // Distance from pixel to waveform line
        float dist = abs(uv.y - wave_y);
        float thickness_px = u_wave_thickness / u_resolution.y;

        // Glowing line (inverse-distance falloff)
        float glow = thickness_px / (dist + thickness_px * 0.5);
        glow = pow(glow, 2.5) * 0.4;

        // Color shifts with time
        vec3 wc = u_wave_color;
        wc = mix(wc, wc.gbr, 0.3 * sin(u_time * 0.5));

        composite += wc * glow * (1.0 + u_beat * 0.8);
    }

    // --- Spectrum ring ---
    if (u_ring_enabled > 0.5) {
        float radius = length(p);
        float angle = atan(p.y, p.x);
        float norm_angle = (angle + PI) / TAU;

        // Sample FFT at this angle
        float magnitude = texture(u_fft, norm_angle).r;

        // Ring parameters
        float inner = u_ring_radius + u_beat * 0.03;
        float bar_length = magnitude * 0.4 * (1.0 + u_bass * 0.5);
        float outer = inner + bar_length;

        // Check if pixel is in a ring bar
        if (radius > inner && radius < outer) {
            float bar_pos = (radius - inner) / max(bar_length, 0.001);
            // Color gradient along bar
            vec3 ring_col = mix(
                vec3(0.1, 0.4, 1.0),
                vec3(1.0, 0.3, 0.7),
                bar_pos
            );
            ring_col *= 1.0 + u_beat * 0.5;

            // Soft edges
            float edge = smoothstep(inner, inner + 0.01, radius);
            edge *= smoothstep(outer, outer - 0.01, radius);

            composite += ring_col * edge * u_ring_glow;
        }

        // Inner ring glow
        float ring_glow = 0.003 / (abs(radius - inner) + 0.003);
        composite += vec3(0.2, 0.1, 0.4) * ring_glow * 0.15;
    }

    // --- Beat particles ---
    if (u_particles_enabled > 0.5 && u_beat > 0.1) {
        int count = int(u_particle_count);
        for (int i = 0; i < 64; i++) {
            if (i >= count) break;
            float fi = float(i);
            // Particle position expands outward from center on beat
            float angle = hash(fi * 7.13 + 0.5) * TAU;
            float speed = hash(fi * 3.77 + 1.3) * 0.8 + 0.2;
            float expand = (1.0 - u_beat) * speed * 1.5;
            vec2 particle_pos = vec2(cos(angle), sin(angle)) * expand;

            float dist = length(p - particle_pos);
            float size_px = u_particle_size / u_resolution.y;
            float bright = size_px / (dist + size_px);
            bright = pow(bright, 3.0) * u_beat;

            // Per-particle color
            float hue = hash(fi * 1.37) + u_time * 0.1;
            vec3 pc = vec3(
                0.5 + 0.5 * cos(hue * TAU),
                0.5 + 0.5 * cos(hue * TAU + 2.094),
                0.5 + 0.5 * cos(hue * TAU + 4.189)
            );
            composite += pc * bright * 0.5;
        }
    }

    // --- Center flash on hard beat ---
    float center_dist = length(p);
    float flash = exp(-center_dist * 3.0) * u_beat * u_beat * 0.4;
    composite += vec3(0.6, 0.4, 0.9) * flash;

    frag_color = vec4(composite, 1.0);
}
