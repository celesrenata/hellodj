#version 330 core

// Hypnotic — Concentric rotating rings with moiré interference.
// Each ring rotates at speed from iBandEnergy[ring%7]. Ring thickness
// oscillates with sin(iTime+ring). Beat expands all radii. HSV color shift.

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
#define NUM_RINGS 14

// --- Self-contained HSV→RGB conversion ---

vec3 hsv2rgb(vec3 c) {
    vec3 p = abs(fract(c.xxx + vec3(1.0, 2.0/3.0, 1.0/3.0)) * 6.0 - 3.0);
    return c.z * mix(vec3(1.0), clamp(p - 1.0, 0.0, 1.0), c.y);
}

// --- Rotation matrix ---

mat2 rotate2d(float angle) {
    float c = cos(angle);
    float s = sin(angle);
    return mat2(c, -s, s, c);
}

void main() {
    vec2 uv = vUV;

    // Center coordinates [-1, 1] with aspect correction
    vec2 center = (uv - 0.5) * 2.0;
    center.x *= iResolution.x / iResolution.y;

    // Accumulate color from all rings
    vec3 col = vec3(0.0);

    // Beat expansion burst: shifts all radii outward (Req 7 AC 1 — structural change)
    float beatExpansion = iBeat * 0.3;

    // Speed boost while iBeat > 0.5 (Req 7 AC 2 — second parameter differs)
    float speedBoost = 1.0 + smoothstep(0.5, 1.0, iBeat) * 2.0;

    for (int i = 0; i < NUM_RINGS; i++) {
        float fi = float(i);

        // Ring rotation speed driven by iBandEnergy[ring%7] (Req 7 AC 3 — all 7 bands)
        int bandIdx = i - (i / 7) * 7; // i % 7 without modulo
        float bandEnergy = iBandEnergy[bandIdx];
        float rotSpeed = (0.2 + bandEnergy * 2.0) * speedBoost;

        // Alternate rotation direction for adjacent rings
        float direction = (i - (i / 2) * 2 == 0) ? 1.0 : -1.0; // even/odd
        float angle = iTime * rotSpeed * direction * 0.5;

        // Rotate the sampling point per-ring for moiré interference
        vec2 rotated = rotate2d(angle) * center;

        // Distance from center
        float dist = length(rotated);

        // Ring radius: evenly spaced + beat expansion
        float ringRadius = (fi + 1.0) / float(NUM_RINGS + 1) * 1.4 + beatExpansion;

        // Ring thickness oscillates with sin(iTime + ring_index) (continuous motion)
        float thickness = 0.02 + 0.015 * sin(iTime * 0.8 + fi * 1.1);

        // Ring intensity: smooth ring shape (distance from ring radius)
        float ringDist = abs(dist - ringRadius);
        float ring = smoothstep(thickness, thickness * 0.3, ringDist);

        // Moiré interference: angular modulation creates pattern within rings
        float theta = atan(rotated.y, rotated.x);
        float moire = 0.5 + 0.5 * sin(theta * (6.0 + fi * 2.0) + iTime * direction);
        ring *= 0.6 + 0.4 * moire;

        // HSV color shift: hue driven by ring depth + time (Req 8 AC 1)
        float hue = fract(fi / float(NUM_RINGS) + iTime * 0.05 + bandEnergy * 0.3);
        float sat = 0.7 + 0.3 * sin(iTime + fi);
        float val = ring;

        vec3 ringColor = hsv2rgb(vec3(hue, sat, val));

        // Additive blending of rings creates interference patterns
        col += ringColor * 0.7;
    }

    // Center glow on beat
    float r = length(center);
    float centerGlow = 0.015 / (r + 0.015);
    col += vec3(centerGlow * iBeat * 0.4 * iGlowIntensity);

    // Vignette: darken edges
    float vignette = 1.0 - smoothstep(0.8, 1.8, r);
    col *= vignette;

    // Background opacity blend
    col *= iBgOpacity;

    // Clamp to prevent oversaturation from additive blending
    col = clamp(col, 0.0, 1.0);

    // Continuous motion from iTime even with zero audio (Req 8 AC 1):
    // Ring rotation (iTime * rotSpeed), thickness oscillation (sin(iTime + fi)),
    // hue cycling (iTime * 0.05), and moiré modulation (iTime * direction)
    // all incorporate iTime directly.

    fragColor = vec4(col, 1.0);
}
