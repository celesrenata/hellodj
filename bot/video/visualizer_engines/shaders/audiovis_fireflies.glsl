#version 330 core

// Fireflies — Distance-field particle glow (soft luminous points).
// N particle positions with Brownian noise drift. Count scales with audio energy.
// Beat causes radial scatter outward from center. Warm gold/amber/green palette
// via per-particle hash. Loop capped at 32 particles (Req 9 AC 2).
// Continuous motion from iTime alone (Req 8 AC 1).

in vec2 vUV;
out vec4 fragColor;

uniform float     iTime;
uniform vec2      iResolution;
uniform float     iBeat;
uniform float     iBPM;
uniform float     iBandEnergy[7];
uniform sampler1D iFFT;
uniform int       iFFTBins;
uniform float     iGlowIntensity;
uniform float     iBgOpacity;

// --- Self-contained hash/noise utilities ---

float hash11(float p) {
    p = fract(p * 0.1031);
    p *= p + 33.33;
    p *= p + p;
    return fract(p);
}

float hash12(vec2 p) {
    vec3 p3 = fract(vec3(p.xyx) * 0.1031);
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.x + p3.y) * p3.z);
}

vec2 hash22(vec2 p) {
    vec3 p3 = fract(vec3(p.xyx) * vec3(0.1031, 0.1030, 0.0973));
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.xx + p3.yz) * p3.zy);
}

// Value noise for Brownian drift
float noise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    float a = hash12(i);
    float b = hash12(i + vec2(1.0, 0.0));
    float c = hash12(i + vec2(0.0, 1.0));
    float d = hash12(i + vec2(1.0, 1.0));
    return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
}

void main() {
    vec2 uv = vUV;
    float aspect = iResolution.x / iResolution.y;
    vec2 uvCorrected = vec2((uv.x - 0.5) * aspect, uv.y - 0.5);

    // --- Audio energy sum for particle count (Req 7 AC 3: uses bands 0,2,4,5,6) ---
    // Uses minimum 3 distinct iBandEnergy bands
    float energySum = iBandEnergy[0] + iBandEnergy[2] + iBandEnergy[4]
                    + iBandEnergy[5] + iBandEnergy[6];

    // Visible particle count scales with energy: N = 8 + int(energySum * 20.0)
    // Capped at 32 (Req 9 AC 2)
    int N = 8 + int(energySum * 20.0);
    N = min(N, 32);

    // Beat parameters (Req 7 AC 1: structural scatter, Req 7 AC 2: 2+ params differ)
    // When iBeat > 0.5: scatter strength + glow radius both change
    float beatScatter = iBeat * 0.35;  // radial burst strength
    float beatGlow = 1.0 + iBeat * 1.5; // glow size amplification on beat

    // iBandEnergy[5] controls particle velocity magnitude (presence band)
    float velocityMod = 0.5 + iBandEnergy[5] * 2.0;

    // Accumulated color
    vec3 col = vec3(0.0);

    // Loop over particles (max 32 iterations, Req 9 AC 2)
    for (int i = 0; i < 32; i++) {
        if (i >= N) break;

        float fi = float(i);

        // --- Per-particle base position from hash (deterministic seed) ---
        vec2 baseOffset = hash22(vec2(fi * 1.73, fi * 2.91));

        // --- Brownian noise drift for continuous motion (Req 8 AC 1) ---
        // Noise-based velocity: sample noise at particle-specific coords + time
        // Gives smooth, organic wandering independent of audio
        float driftX = noise(vec2(fi * 3.7 + iTime * 0.3 * velocityMod, fi * 1.1)) - 0.5;
        float driftY = noise(vec2(fi * 2.3, fi * 4.9 + iTime * 0.25 * velocityMod)) - 0.5;
        vec2 drift = vec2(driftX, driftY) * 0.8;

        // Particle position: base + drift, wrapped to stay in view
        vec2 pos = fract(baseOffset + drift) - 0.5;
        pos.x *= aspect;

        // --- Beat scatter: push particles outward from center (Req 7 AC 1) ---
        // Structural change: particle positions radially displaced on beat
        vec2 scatterDir = normalize(pos + vec2(0.001));
        pos += scatterDir * beatScatter * (0.5 + hash11(fi * 7.13));

        // --- Distance-field glow: inverse distance ---
        float dist = length(uvCorrected - pos);

        // Glow radius varies per particle + beat amplification
        float baseRadius = 0.008 + 0.005 * hash11(fi * 3.37);
        float radius = baseRadius * beatGlow;
        float glow = radius / (dist + 0.001);
        glow = glow * glow; // sharper falloff for firefly look

        // Soft clamp to avoid extreme brightness at center
        glow = min(glow, 8.0);

        // --- Warm color palette: gold, amber, soft green per-particle hash ---
        float colorHash = hash11(fi * 5.71 + 0.3);
        vec3 particleColor;
        if (colorHash < 0.4) {
            // Gold: warm yellow-orange
            particleColor = vec3(1.0, 0.85, 0.2);
        } else if (colorHash < 0.75) {
            // Amber: deeper orange
            particleColor = vec3(1.0, 0.6, 0.1);
        } else {
            // Soft green: organic warmth
            particleColor = vec3(0.5, 0.9, 0.3);
        }

        // Slight color variation over time for liveliness
        float colorPulse = 0.85 + 0.15 * sin(iTime * (1.0 + fi * 0.1) + fi);
        particleColor *= colorPulse;

        // Accumulate particle glow
        col += particleColor * glow * 0.15;
    }

    // --- Subtle background: very dark warm tone with faint gradient ---
    vec3 bg = mix(vec3(0.01, 0.01, 0.02), vec3(0.02, 0.015, 0.005), uv.y);
    col += bg;

    // Glow intensity uniform
    col *= (0.7 + iGlowIntensity * 0.5);

    // Background opacity blend
    col *= iBgOpacity;

    fragColor = vec4(col, 1.0);
}
