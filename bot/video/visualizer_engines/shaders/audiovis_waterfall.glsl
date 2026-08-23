#version 330 core

// Waterfall (spectrogram) fragment shader for AudioVis engine.
// Renders a scrolling frequency-time spectrogram with color mapping.

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

// Heat map color: black → blue → cyan → green → yellow → red → white
vec3 heatmap(float t) {
    t = clamp(t, 0.0, 1.0);
    vec3 col = vec3(0.0);
    col += vec3(0.0, 0.0, 1.0) * smoothstep(0.0, 0.2, t);
    col = mix(col, vec3(0.0, 1.0, 1.0), smoothstep(0.2, 0.4, t));
    col = mix(col, vec3(0.0, 1.0, 0.0), smoothstep(0.35, 0.5, t));
    col = mix(col, vec3(1.0, 1.0, 0.0), smoothstep(0.5, 0.7, t));
    col = mix(col, vec3(1.0, 0.0, 0.0), smoothstep(0.7, 0.9, t));
    col = mix(col, vec3(1.0, 1.0, 1.0), smoothstep(0.9, 1.0, t));
    return col;
}

void main() {
    vec2 uv = vUV;

    // Background
    vec3 bg = vec3(0.0, 0.0, 0.02) * iBgOpacity;

    // Current FFT line (rendered as the bottom row, scrolling up simulated by time)
    // In a real waterfall, we'd use a 2D texture history. Here we approximate
    // using the current FFT data with time-based scrolling effect.
    float freq = uv.x;
    float magnitude = texture(iFFT, freq).r;

    // Simulate scrolling: current data strongest at bottom, fading up
    float scrollPos = fract(iTime * 0.5);  // scroll speed
    float age = uv.y;  // 0=bottom (newest), 1=top (oldest)

    // Fade intensity with age
    float fadedMag = magnitude * exp(-age * 2.5);

    // Beat pulse brightens current data
    fadedMag *= 1.0 + iBeat * 0.4 * (1.0 - age);

    // Apply heatmap coloring
    vec3 col = heatmap(fadedMag * 1.5);
    col *= (1.0 - age * 0.3);  // darken older data

    // Glow effect
    col += col * iGlowIntensity * 0.2 * fadedMag;

    // Mix with background
    float alpha = smoothstep(0.01, 0.05, fadedMag);
    col = mix(bg, col, alpha);

    // Frequency axis markers
    float freqMark = smoothstep(0.003, 0.0, abs(mod(uv.x * 16.0, 1.0) - 0.5) - 0.49);
    col += vec3(0.1, 0.1, 0.15) * freqMark * 0.2 * (1.0 - age);

    fragColor = vec4(col, 1.0);
}
