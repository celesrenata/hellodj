#version 330 core

// Waterfall/spectrogram fragment shader for AudioVis engine.
// Renders current FFT as a horizontal band at the top, scrolling down over time.
// Uses time-based UV offset to simulate scrolling without a history buffer.

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
    col = mix(col, vec3(1.0, 0.0, 0.0), smoothstep(0.65, 0.85, t));
    col = mix(col, vec3(1.0, 1.0, 1.0), smoothstep(0.85, 1.0, t));
    return col;
}

void main() {
    vec2 uv = vUV;

    // Background
    vec3 bg = vec3(0.01, 0.01, 0.02) * iBgOpacity;

    // The waterfall effect: the top portion shows the current FFT,
    // the rest fades to show "history" (simulated via time-shifted sampling)
    // In a real implementation, you'd use a 2D texture ring buffer.
    // Here we simulate by using the FFT with time-based color decay.

    // X axis = frequency, Y axis = time (top = now, bottom = past)
    float freq = uv.x;
    float age = 1.0 - uv.y; // 0 at top (current), 1 at bottom (oldest)

    // Sample current FFT
    float magnitude = texture(iFFT, freq).r;

    // Decay with age — simulate scrolling history
    float decayedMag = magnitude * exp(-age * 3.0);

    // Add subtle time animation to make it feel alive even without a buffer
    float timeWobble = sin(iTime * 0.5 + freq * 10.0) * 0.02;
    decayedMag += timeWobble * (1.0 - age);

    decayedMag = clamp(decayedMag, 0.0, 1.0);

    // Apply heatmap coloring
    vec3 col = heatmap(decayedMag * 1.5);

    // Beat pulse: brighten the top rows
    if (age < 0.05) {
        col *= 1.0 + iBeat * 1.0;
    }

    // Scanline effect (subtle horizontal lines)
    float scanline = 0.95 + 0.05 * sin(uv.y * iResolution.y * 0.5);
    col *= scanline;

    // Fade bottom to black
    col *= smoothstep(1.0, 0.7, age);

    // Mix with background for very low magnitudes
    col = mix(bg, col, smoothstep(0.0, 0.02, decayedMag));

    fragColor = vec4(col, 1.0);
}
