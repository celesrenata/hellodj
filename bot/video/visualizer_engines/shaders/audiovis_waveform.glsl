#version 330 core

// Waveform fragment shader for AudioVis engine.
// Renders a scrolling waveform line with glow and beat pulse.

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

void main() {
    vec2 uv = vUV;

    // Background gradient
    vec3 bg = mix(
        vec3(0.01, 0.01, 0.04),
        vec3(0.03, 0.01, 0.06),
        uv.y
    ) * iBgOpacity;

    // Sample FFT at this x position
    float fftVal = texture(iFFT, uv.x).r;

    // Waveform center line with amplitude
    float waveY = 0.5 + (fftVal - 0.3) * 0.6;

    // Beat pulse makes waveform thicker
    float lineWidth = 0.008 + iBeat * 0.006;

    // Distance from pixel to waveform line
    float dist = abs(uv.y - waveY);

    // Core line
    float line = smoothstep(lineWidth, lineWidth * 0.3, dist);

    // Glow around line
    float glow = exp(-dist * dist / (0.002 * iGlowIntensity + 0.0001)) * 0.4 * iGlowIntensity;

    // Color based on frequency position
    vec3 lineColor = mix(
        vec3(0.2, 0.6, 1.0),
        vec3(1.0, 0.3, 0.6),
        uv.x
    );

    // Beat brightness boost
    lineColor *= 1.0 + iBeat * 0.6;

    vec3 col = bg;
    col += lineColor * line;
    col += lineColor * glow;

    // Subtle grid lines
    float gridX = smoothstep(0.002, 0.0, abs(mod(uv.x * float(iFFTBins), 1.0) - 0.5) - 0.48);
    float gridY = smoothstep(0.002, 0.0, abs(mod(uv.y * 8.0, 1.0) - 0.5) - 0.48);
    col += vec3(0.05, 0.05, 0.1) * (gridX + gridY) * 0.3;

    fragColor = vec4(col, 1.0);
}
