#version 330 core

// Ocean — Caustic light refraction (underwater light patterns).
// Sum of 3 rotated sine-wave grids at different frequencies.
// iBandEnergy[0..1] (sub-bass/bass) controls wave frequency modulation.
// Beat triggers circular ripple expansion from center, decays over 1 second.
// Dark blue depth gradient with brightness attenuation.
// Gentle continuous sway via time-offset UV coordinates (Req 8 AC 1).

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

// --- Self-contained utilities ---

mat2 rotate2d(float angle) {
    float c = cos(angle);
    float s = sin(angle);
    return mat2(c, -s, s, c);
}

float hash(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
}

float noise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    float a = hash(i);
    float b = hash(i + vec2(1.0, 0.0));
    float c = hash(i + vec2(0.0, 1.0));
    float d = hash(i + vec2(1.0, 1.0));
    return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
}

// Caustic pattern: sum of 3 rotated sine grids
float caustic(vec2 uv, float time, float freqMod) {
    float result = 0.0;

    // Grid 1: base frequency, no rotation
    float f1 = 8.0 + freqMod * 4.0;
    vec2 uv1 = uv;
    result += max(sin(uv1.x * f1 + time) + sin(uv1.y * f1 * 1.3 + time * 0.7), 0.0);

    // Grid 2: higher frequency, rotated 60 degrees
    float f2 = 10.0 + freqMod * 5.0;
    vec2 uv2 = rotate2d(PI / 3.0) * uv;
    result += max(sin(uv2.x * f2 + time * 1.1) + sin(uv2.y * f2 * 1.2 + time * 0.8), 0.0);

    // Grid 3: different frequency, rotated -45 degrees
    float f3 = 7.0 + freqMod * 3.5;
    vec2 uv3 = rotate2d(-PI / 4.0) * uv;
    result += max(sin(uv3.x * f3 + time * 0.9) + sin(uv3.y * f3 * 1.4 + time * 1.2), 0.0);

    // Normalize and shape
    return result / 3.0;
}

void main() {
    vec2 uv = vUV;
    float aspect = iResolution.x / iResolution.y;

    // Center coordinates for ripple calculations
    vec2 center = (uv - 0.5) * 2.0;
    center.x *= aspect;

    // --- Continuous sway from iTime UV offsets (Req 8 AC 1) ---
    // Ensures perceptible frame-to-frame change even with zero audio
    vec2 swayUV = uv;
    swayUV.x += sin(iTime * 0.3) * 0.02 + sin(iTime * 0.17) * 0.015;
    swayUV.y += cos(iTime * 0.23) * 0.02 + cos(iTime * 0.13) * 0.01;

    // --- Frequency modulation from iBandEnergy[0..1] (sub-bass/bass) ---
    // Controls wave height/frequency (Req 7 AC 3: uses 3+ distinct bands)
    float freqMod = iBandEnergy[0] * 0.8 + iBandEnergy[1] * 0.6;

    // Also incorporate iBandEnergy[2] for additional depth variation (Req 7 AC 3: 3 distinct bands)
    float midEnergy = iBandEnergy[2];

    // --- Caustic pattern computation ---
    float time = iTime * 0.6;
    float c = caustic(swayUV, time, freqMod);

    // Secondary caustic layer for depth (slightly offset, slower)
    float c2 = caustic(swayUV * 1.5 + vec2(1.7, 2.3), time * 0.7, freqMod * 0.8);

    // Blend caustic layers — deeper layer is dimmer
    float causticFinal = c * 0.7 + c2 * 0.3;

    // --- Beat-triggered circular ripple from center (Req 7 AC 1: structural change) ---
    // Ripple expands outward when iBeat spikes, decays over ~1 second
    float dist = length(center);

    // Ripple phase: beat drives initial burst, expands with decaying iBeat
    // When iBeat is high, ripple is close to center; as it decays, ripple expands
    float rippleRadius = (1.0 - iBeat) * 2.5; // expands from 0 to 2.5 as beat decays
    float rippleWidth = 0.15;
    float ripple = smoothstep(rippleWidth, 0.0, abs(dist - rippleRadius)) * iBeat;

    // Multiple concentric ripple rings for visual richness
    float ripple2 = smoothstep(rippleWidth * 0.8, 0.0, abs(dist - rippleRadius * 0.6)) * iBeat * 0.5;
    float totalRipple = ripple + ripple2;

    // --- Dark blue depth gradient background ---
    // Deeper at bottom, lighter toward top (simulating underwater light)
    vec3 deepBlue = vec3(0.01, 0.03, 0.12);
    vec3 shallowBlue = vec3(0.03, 0.10, 0.25);
    vec3 bgColor = mix(deepBlue, shallowBlue, uv.y);

    // Depth-based brightness attenuation (darker at edges/bottom)
    float depthAtten = 0.5 + 0.5 * uv.y;
    depthAtten *= 1.0 - 0.3 * smoothstep(0.0, 1.5, dist); // vignette

    // Mid-band energy adds subtle depth variation
    depthAtten += midEnergy * 0.15;

    // --- Compose final color ---
    // Caustic light color: aqua/cyan tones on dark blue
    vec3 causticColor = vec3(0.15, 0.55, 0.70) * causticFinal;

    // Add warmer caustic highlights
    vec3 highlightColor = vec3(0.25, 0.70, 0.50) * pow(causticFinal, 2.0) * 0.5;

    // Combine
    vec3 col = bgColor + (causticColor + highlightColor) * depthAtten;

    // --- Apply ripple (Req 7 AC 1: circular expansion = structural change) ---
    // Ripple adds bright ring of caustic light
    vec3 rippleColor = vec3(0.3, 0.7, 0.9) * totalRipple;
    col += rippleColor;

    // --- Peak state (Req 7 AC 2): while iBeat > 0.5, 2+ parameters differ ---
    // Parameter 1: overall brightness boost
    // Parameter 2: caustic contrast enhancement
    float peakFactor = smoothstep(0.5, 1.0, iBeat);
    col *= 1.0 + peakFactor * 0.8;                    // brightness boost
    col += causticColor * peakFactor * 0.4;            // caustic contrast boost

    // --- Subtle noise shimmer for underwater particle feel ---
    float shimmer = noise(swayUV * 50.0 + iTime * 2.0) * 0.02;
    col += vec3(shimmer * 0.5, shimmer * 0.8, shimmer);

    // --- Glow intensity uniform ---
    col *= (0.8 + iGlowIntensity * 0.3);

    // --- Background opacity ---
    col *= iBgOpacity;

    fragColor = vec4(col, 1.0);
}
