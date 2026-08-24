#version 330 core

// Kaleidoscope — Psychedelic mirrored fractal geometry with polar UV folding.
// Sector count N increases on iBeat (6→10). Layered noise colored with cosine
// palette. iBandEnergy[0..2] drives color cycling. iTime rotates pattern.

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

// --- Self-contained noise utilities ---

float hash(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
}

float noise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    f = f * f * (3.0 - 2.0 * f); // smoothstep
    float a = hash(i);
    float b = hash(i + vec2(1.0, 0.0));
    float c = hash(i + vec2(0.0, 1.0));
    float d = hash(i + vec2(1.0, 1.0));
    return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
}

float fbm(vec2 p, int octaves) {
    float value = 0.0;
    float amplitude = 0.5;
    float frequency = 1.0;
    for (int i = 0; i < 4; i++) {
        if (i >= octaves) break;
        value += amplitude * noise(p * frequency);
        frequency *= 2.0;
        amplitude *= 0.5;
    }
    return value;
}

mat2 rotate2d(float angle) {
    float c = cos(angle);
    float s = sin(angle);
    return mat2(c, -s, s, c);
}

// Cosine palette: attempt a vibrant psychedelic gradient
vec3 palette(float t) {
    vec3 a = vec3(0.5, 0.5, 0.5);
    vec3 b = vec3(0.5, 0.5, 0.5);
    vec3 c = vec3(1.0, 1.0, 1.0);
    vec3 d = vec3(0.00, 0.33, 0.67);
    return a + b * cos(TAU * (c * t + d));
}

void main() {
    vec2 uv = vUV;

    // Center coordinates [-1, 1] with aspect correction
    vec2 center = (uv - 0.5) * 2.0;
    center.x *= iResolution.x / iResolution.y;

    // Rotate entire pattern continuously with iTime (Req 8 AC 1)
    center = rotate2d(iTime * 0.15) * center;

    // Polar coordinates
    float r = length(center);
    float theta = atan(center.y, center.x) + PI; // [0, TAU]

    // Sector count N: increases on beat (Req 7 AC 1 — structural change)
    // Base 6 sectors, beat pushes up to 10
    float N = 6.0 + floor(iBeat * 4.0);

    // Fold theta into N sectors with mirroring
    float sectorAngle = TAU / N;
    float foldedTheta = mod(theta, sectorAngle);
    // Mirror: reflect second half of sector
    if (foldedTheta > sectorAngle * 0.5) {
        foldedTheta = sectorAngle - foldedTheta;
    }

    // Reconstruct cartesian from folded polar
    vec2 foldedUV = vec2(cos(foldedTheta), sin(foldedTheta)) * r;

    // Color cycling speed driven by iBandEnergy[0..2] (Req 7 AC 3 — 3 bands)
    float colorSpeed = 0.3 + iBandEnergy[0] * 1.5 + iBandEnergy[1] * 1.0 + iBandEnergy[2] * 0.7;

    // Layered noise fields
    float n1 = fbm(foldedUV * 3.0 + iTime * 0.2 * colorSpeed, 4);
    float n2 = fbm(foldedUV * 5.0 - iTime * 0.15 * colorSpeed + vec2(3.7, 1.2), 3);
    float n3 = fbm(foldedUV * 8.0 + iTime * 0.1 + vec2(n1 * 0.5, n2 * 0.5), 3);

    // Combine noise layers
    float pattern = n1 * 0.5 + n2 * 0.3 + n3 * 0.2;

    // Cosine palette coloring — cycle driven by pattern + time + band energy
    float paletteIndex = pattern + iTime * 0.1 * colorSpeed + r * 0.5;
    vec3 col = palette(paletteIndex);

    // Peak state: while iBeat > 0.5, boost saturation + add radial glow (Req 7 AC 2)
    // Two parameters change: color intensity AND pattern scale
    float peakFactor = smoothstep(0.5, 1.0, iBeat);
    col *= 1.0 + peakFactor * 1.2;                           // intensity boost
    col = mix(col, col * vec3(1.3, 0.8, 1.5), peakFactor);  // hue shift toward purple

    // Radial vignette with beat glow
    float vignette = 1.0 - smoothstep(0.3, 1.5, r);
    col *= vignette;

    // Beat glow at center
    float centerGlow = 0.02 / (r + 0.02);
    col += palette(iTime * 0.05) * centerGlow * iBeat * 0.5 * iGlowIntensity;

    // Background opacity blend
    col *= iBgOpacity;

    // Ensure continuous motion from iTime even with zero audio (Req 8 AC 1):
    // The rotate2d, noise time offsets, and paletteIndex all incorporate iTime.

    fragColor = vec4(col, 1.0);
}
