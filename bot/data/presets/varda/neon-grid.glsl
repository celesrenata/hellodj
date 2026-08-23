#version 330 core

// Retro 80s grid with bass-rippled surface.
// Shadertoy-compatible uniform convention.

uniform float     iTime;
uniform vec2      iResolution;
uniform sampler2D iChannel0;    // 512x2 audio texture (row 0: waveform, row 1: FFT)
uniform float     iBeat;        // 0-1 decaying beat pulse
uniform float     iBPM;
uniform float     iBandEnergy[7];

in vec2 fragCoord;
out vec4 fragColor;

void main() {
    vec2 uv = fragCoord;
    vec2 p = uv * 2.0 - 1.0;
    p.x *= iResolution.x / iResolution.y;

    float bass = iBandEnergy[0] + iBandEnergy[1];
    float mid = iBandEnergy[3] + iBandEnergy[4];

    // Perspective-projected grid
    float horizon = 0.3;
    float y = uv.y - horizon;

    vec3 color = vec3(0.0);

    if (y < 0.0) {
        // Sky gradient (dark purple to black)
        float skyGrad = 1.0 - (uv.y / horizon);
        color = mix(vec3(0.0), vec3(0.1, 0.0, 0.2), skyGrad);

        // Sun
        float sunDist = length(vec2(p.x, uv.y - horizon + 0.1));
        float sun = smoothstep(0.15, 0.14, sunDist);
        // Scanlines on sun
        sun *= step(0.5, fract(uv.y * 40.0));
        color += vec3(1.0, 0.3, 0.1) * sun;
    } else {
        // Ground plane (perspective grid)
        float depth = 0.5 / (y + 0.001);
        float scrollZ = depth + iTime * (2.0 + bass * 3.0);
        float scrollX = p.x * depth;

        // Bass-driven ripple displacement
        float ripple = sin(scrollZ * 0.5 + iTime * 2.0) * bass * 0.3;
        scrollX += ripple;

        // Grid lines
        float gridX = abs(fract(scrollX) - 0.5);
        float gridZ = abs(fract(scrollZ * 0.5) - 0.5);

        float lineX = smoothstep(0.02, 0.0, gridX / depth * 2.0);
        float lineZ = smoothstep(0.02, 0.0, gridZ);

        float grid = max(lineX, lineZ);

        // Neon coloring
        vec3 gridColor = mix(vec3(0.0, 0.8, 1.0), vec3(1.0, 0.0, 0.8), mid);
        color = gridColor * grid;

        // Beat pulse on grid
        color *= 1.0 + iBeat * 1.5;

        // Distance fog
        float fog = exp(-y * 3.0);
        color *= fog;
    }

    fragColor = vec4(color, 1.0);
}
