#version 330 core

// Spectrum bars fragment shader for AudioVis engine.
// Renders vertical frequency bars with glow and beat pulse.

in vec2 vUV;
out vec4 fragColor;

uniform float     iTime;
uniform vec2      iResolution;
uniform float     iBeat;          // 0-1 decaying beat pulse
uniform float     iBPM;
uniform float     iBandEnergy[7];
uniform sampler1D iFFT;           // 512-bin FFT magnitude
uniform int       iFFTBins;       // display bins count
uniform float     iGlowIntensity;
uniform float     iBgOpacity;

// Color palette based on frequency
vec3 barColor(float freq, float intensity) {
    vec3 low  = vec3(0.1, 0.4, 1.0);   // blue for bass
    vec3 mid  = vec3(0.0, 1.0, 0.4);   // green for mids
    vec3 high = vec3(1.0, 0.2, 0.5);   // pink for treble
    vec3 col = mix(low, mid, smoothstep(0.0, 0.5, freq));
    col = mix(col, high, smoothstep(0.5, 1.0, freq));
    return col * (0.7 + 0.3 * intensity);
}

void main() {
    vec2 uv = vUV;

    // Background
    vec3 bg = vec3(0.02, 0.02, 0.05) * iBgOpacity;

    // Bar parameters
    int numBars = iFFTBins;
    float barWidth = 1.0 / float(numBars);
    float gap = barWidth * 0.15;

    // Determine which bar this pixel falls in
    int barIndex = int(uv.x / barWidth);
    float barLocalX = mod(uv.x, barWidth);

    // Skip gaps between bars
    if (barLocalX < gap || barLocalX > barWidth - gap) {
        fragColor = vec4(bg, 1.0);
        return;
    }

    // Sample FFT for this bar
    float freq = (float(barIndex) + 0.5) / float(numBars);
    float magnitude = texture(iFFT, freq).r;

    // Beat pulse: expand bars and boost brightness
    float beatBoost = iBeat * 0.3;
    float barHeight = magnitude * (0.85 + beatBoost);

    // Draw bar
    if (uv.y < barHeight) {
        float intensity = uv.y / barHeight;
        vec3 col = barColor(freq, intensity);

        // Brightness boost from beat
        col *= 1.0 + iBeat * 0.5;

        // Glow effect at bar top
        float topGlow = smoothstep(barHeight - 0.02 * iGlowIntensity, barHeight, uv.y);
        col += vec3(0.3, 0.5, 1.0) * topGlow * iGlowIntensity;

        fragColor = vec4(col, 1.0);
    } else {
        // Subtle reflection below bars
        float reflDist = uv.y - barHeight;
        if (reflDist < 0.05 && barHeight > 0.01) {
            float reflAlpha = (1.0 - reflDist / 0.05) * 0.15;
            vec3 col = barColor(freq, 0.5) * reflAlpha;
            fragColor = vec4(bg + col, 1.0);
        } else {
            fragColor = vec4(bg, 1.0);
        }
    }
}
