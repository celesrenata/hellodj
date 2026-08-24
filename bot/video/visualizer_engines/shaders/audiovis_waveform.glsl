#version 330 core

// Waveform/oscilloscope fragment shader for AudioVis engine.
// Renders a glowing audio waveform line with trail decay.

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
        vec3(0.02, 0.01, 0.05),
        vec3(0.05, 0.02, 0.08),
        uv.y
    ) * iBgOpacity;

    // Sample FFT at this x position to get waveform height
    float freq = uv.x;
    float magnitude = texture(iFFT, freq).r;

    // Waveform center line at y=0.5, amplitude mapped to [-0.4, 0.4]
    float waveY = 0.5 + magnitude * 0.4 * sin(uv.x * 6.2832 * 3.0 + iTime * 2.0);

    // Distance from pixel to waveform line
    float dist = abs(uv.y - waveY);

    // Glow effect — thicker line with soft falloff
    float lineWidth = 0.003 + 0.002 * iBeat;
    float glow = lineWidth / (dist + 0.001);
    glow = pow(glow, 1.5) * 0.15;

    // Color based on frequency position + beat pulse
    vec3 lineColor = vec3(0.2, 0.6, 1.0);
    lineColor = mix(lineColor, vec3(1.0, 0.3, 0.6), uv.x);
    lineColor *= 1.0 + iBeat * 0.8;

    // Secondary waveform (reflected, dimmer)
    float waveY2 = 0.5 - magnitude * 0.3 * sin(uv.x * 6.2832 * 2.0 + iTime * 1.5);
    float dist2 = abs(uv.y - waveY2);
    float glow2 = lineWidth / (dist2 + 0.001);
    glow2 = pow(glow2, 1.5) * 0.06;
    vec3 lineColor2 = vec3(0.1, 1.0, 0.5) * (1.0 + iBeat * 0.4);

    // Combine
    vec3 col = bg + lineColor * glow + lineColor2 * glow2;

    // Beat flash overlay
    col += vec3(0.1, 0.05, 0.15) * iBeat * 0.3;

    fragColor = vec4(col, 1.0);
}
