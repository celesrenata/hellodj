#version 330 core

// Julia set fractal fragment shader for AudioVis engine.
// Continuous zoom via exp(-iTime*0.3), beat-accelerated zoom,
// smooth iteration count coloring with cosine palette driven by iBandEnergy[3..5].

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

#define PI 3.14159265359
#define MAX_ITER 128

// Cosine palette: attempt to capture vivid psychedelic colors
// palette(t) = a + b * cos(2*PI * (c*t + d))
vec3 cosinePalette(float t, vec3 a, vec3 b, vec3 c, vec3 d) {
    return a + b * cos(6.28318 * (c * t + d));
}

void main() {
    // Aspect-corrected centered coordinates
    vec2 uv = vUV - 0.5;
    uv.x *= iResolution.x / iResolution.y;

    // Continuous zoom driven by time (Req 8 AC 1 — motion even with zero audio)
    float zoom = exp(-iTime * 0.3);

    // Beat accelerates zoom (Req 7 AC 1 — structural change on beat)
    // When iBeat > 0.5, distinct peak state with zoom acceleration + color shift (Req 7 AC 2)
    zoom *= 1.0 + iBeat * 2.0;

    // Apply zoom to UV
    vec2 z = uv * zoom * 3.0;

    // Julia constant c orbits slowly — continuous even without audio
    vec2 c = vec2(
        sin(iTime * 0.1) * 0.7,
        cos(iTime * 0.13) * 0.5
    );

    // Beat shifts the Julia constant for structural pattern change (Req 7 AC 2 — 2+ params differ)
    c += vec2(iBeat * 0.15, -iBeat * 0.1);

    // Julia set iteration (max 128 per Req 9 AC 2)
    int iter = 0;
    for (int i = 0; i < MAX_ITER; i++) {
        if (dot(z, z) > 4.0) break;
        // z = z^2 + c
        z = vec2(z.x * z.x - z.y * z.y, 2.0 * z.x * z.y) + c;
        iter++;
    }

    // Smooth iteration count for anti-banding
    float smoothIter = float(iter);
    if (iter < MAX_ITER) {
        float log_zn = log(dot(z, z)) * 0.5;
        float nu = log(log_zn / log(2.0)) / log(2.0);
        smoothIter = float(iter) + 1.0 - nu;
    }

    // Normalize to [0, 1] range
    float t = smoothIter / float(MAX_ITER);

    // Color via cosine palette (Req 7 AC 3 — uses iBandEnergy[3], [4], [5])
    // Palette rotation speed driven by mid/upper-mid/presence bands
    float paletteSpeed = 0.5 + iBandEnergy[3] * 2.0 + iBandEnergy[4] * 1.5 + iBandEnergy[5] * 1.0;
    float paletteOffset = iTime * paletteSpeed * 0.1;

    // Cosine palette with psychedelic parameters
    vec3 col = cosinePalette(
        t + paletteOffset,
        vec3(0.5, 0.5, 0.5),                     // a — brightness center
        vec3(0.5, 0.5, 0.5),                     // b — amplitude
        vec3(1.0, 1.0, 1.0),                     // c — frequency
        vec3(0.00, 0.10, 0.20)                   // d — phase offset (cool palette)
    );

    // Interior points (didn't escape) — dark with subtle glow
    if (iter == MAX_ITER) {
        col = vec3(0.01, 0.005, 0.02);
        // Subtle inner glow from beat
        col += vec3(0.05, 0.02, 0.08) * iBeat;
    }

    // Beat brightness boost (Req 7 AC 2 — peak state visual difference)
    col *= 1.0 + iBeat * 0.8;

    // Glow intensity modulation
    col *= 1.0 + iGlowIntensity * 0.3;

    // Apply background opacity for compositing
    float alpha = mix(iBgOpacity, 1.0, step(0.01, t));

    fragColor = vec4(col, alpha);
}
