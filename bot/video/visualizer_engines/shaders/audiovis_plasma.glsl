#version 330 core

// Multi-frequency sinusoidal color mixing (classic plasma) for AudioVis engine.
// Frequencies modulated by iBandEnergy[0..2]. Beat triggers color inversion flash
// blended over ~6 frames. Continuous flow from iTime.

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
#define TAU 6.28318530718

// --- Inline utility: cosine palette ---
vec3 palette(float t) {
    // Attempt a vibrant cycling palette (purple/teal/magenta/gold)
    vec3 a = vec3(0.5, 0.5, 0.5);
    vec3 b = vec3(0.5, 0.5, 0.5);
    vec3 c = vec3(1.0, 1.0, 1.0);
    vec3 d = vec3(0.263, 0.416, 0.557);
    return a + b * cos(TAU * (c * t + d));
}

void main() {
    // UV in [0,1], centered coords in [-1,1] with aspect correction
    vec2 uv = vUV;
    vec2 p = (uv - 0.5) * 2.0;
    p.x *= iResolution.x / iResolution.y;

    // Distance from center (used for radial plasma term)
    float dist = length(p);

    // --- Frequency modulation from iBandEnergy[0..2] ---
    // Base frequencies + band-energy scaling gives audio-reactive plasma movement
    float f1 = 4.0 + iBandEnergy[0] * 6.0;   // sub-bass drives primary X frequency
    float f2 = 3.0 + iBandEnergy[1] * 5.0;   // bass drives primary Y frequency
    float f3 = 5.0 + iBandEnergy[2] * 4.0;   // low-mid drives secondary X frequency
    float f4 = 3.5 + iBandEnergy[0] * 3.0 + iBandEnergy[1] * 2.0; // bass combo drives radial

    // --- Continuous time flow (Req 8 AC 1) ---
    // Even with zero audio, iTime alone drives perceptible motion
    float t = iTime;

    // --- Classic plasma formula (design.md algorithm) ---
    float r_val = sin(p.x * f1 + t) + sin(p.y * f2 + t * 0.7);
    float g_val = sin(p.x * f3 - t * 1.3) + sin(dist * f4 + t);
    float b_val = sin((p.x + p.y) * (f1 + f3) * 0.3 + t * 0.9)
                + sin(dist * f2 * 0.8 - t * 1.1);

    // Normalize from [-2, 2] range to [0, 1]
    vec3 color = vec3(r_val, g_val, b_val) * 0.25 + 0.5;

    // --- Additional palette coloring for richness ---
    float plasmaIndex = (r_val + g_val + b_val) * 0.167 + t * 0.1;
    vec3 paletteColor = palette(plasmaIndex);
    color = mix(color, paletteColor, 0.4);

    // --- Beat-triggered color inversion flash (Req 7 AC 1, AC 2) ---
    // iBeat decays 1.0 → 0.0 over ~200ms (~6 frames at 30fps).
    // Blend inversion over the full iBeat decay for smooth 6-frame flash.
    // Structural change: color inversion is a palette shift, not just brightness.
    float invertStrength = smoothstep(0.0, 1.0, iBeat);
    vec3 invertedColor = 1.0 - color;
    color = mix(color, invertedColor, invertStrength);

    // --- Peak state: additional visual changes while iBeat > 0.5 (Req 7 AC 2) ---
    // Two parameters differ from resting: (1) speed boost, (2) saturation boost
    float peakFactor = smoothstep(0.5, 1.0, iBeat);
    // Boost saturation during peak
    float luminance = dot(color, vec3(0.299, 0.587, 0.114));
    color = mix(color, mix(vec3(luminance), color, 2.0), peakFactor * 0.5);
    // Boost pattern scale/warp during peak (radial push)
    float peakWarp = peakFactor * 0.3;
    float warpedR = sin(p.x * f1 * (1.0 + peakWarp) + t * 1.5)
                  + sin(p.y * f2 * (1.0 + peakWarp) + t);
    color += vec3(0.1, 0.05, 0.15) * warpedR * peakFactor;

    // --- Band diversity: use iBandEnergy[3..6] for glow/detail (Req 7 AC 3) ---
    // Mid/upper-mid drives edge glow intensity
    float midEnergy = iBandEnergy[3] + iBandEnergy[4];
    float glowBoost = midEnergy * iGlowIntensity * 0.3;
    color += vec3(0.05, 0.02, 0.08) * glowBoost;

    // Presence/brilliance adds sparkle highlights
    float highEnergy = iBandEnergy[5] + iBandEnergy[6];
    float sparkle = sin(p.x * 30.0 + t * 5.0) * sin(p.y * 30.0 - t * 3.0);
    sparkle = max(sparkle, 0.0);
    color += vec3(0.8, 0.6, 1.0) * sparkle * highEnergy * 0.15;

    // --- Background opacity blend ---
    color *= iBgOpacity;

    // Clamp output
    color = clamp(color, 0.0, 1.0);

    fragColor = vec4(color, 1.0);
}
