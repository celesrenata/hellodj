#version 330 core

// Varda: Retrowave neon grid — synthwave perspective grid with audio-driven mountains.
// 80s aesthetic with chromatic horizon glow.

uniform float     iTime;
uniform vec2      iResolution;
uniform sampler2D iChannel0;
uniform float     iBeat;
uniform float     iBPM;
uniform float     iBandEnergy[7];

in vec2 fragCoord;
out vec4 fragColor;

float hash(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
}

void main() {
    vec2 uv = fragCoord;
    vec2 p = uv * 2.0 - 1.0;
    p.x *= iResolution.x / iResolution.y;

    float t = iTime * 0.8;
    float bass = iBandEnergy[0] + iBandEnergy[1];
    float mids = iBandEnergy[2] + iBandEnergy[3] + iBandEnergy[4];
    float highs = iBandEnergy[5] + iBandEnergy[6];

    // Sky gradient (dark blue to black up top, horizon glow at center-bottom)
    vec3 sky = vec3(0.0);
    float horizonY = -0.1;

    if (p.y > horizonY) {
        // Upper half — sky with stars
        float skyGrad = (p.y - horizonY) / (1.0 - horizonY);
        sky = mix(vec3(0.05, 0.0, 0.15), vec3(0.01, 0.0, 0.03), skyGrad);

        // Stars
        vec2 starUV = p * 30.0;
        float star = hash(floor(starUV));
        if (star > 0.995) {
            float twinkle = 0.5 + 0.5 * sin(iTime * 5.0 + star * 100.0);
            sky += vec3(0.8, 0.7, 1.0) * twinkle;
        }

        // Sun (large glowing circle at horizon)
        float sunDist = length(p - vec2(0.0, horizonY + 0.2));
        float sun = smoothstep(0.35, 0.0, sunDist);
        // Horizontal scanlines through sun
        float scanlines = step(0.5, fract(p.y * 20.0 - t * 2.0));
        sun *= mix(1.0, 0.3, scanlines * step(sunDist, 0.3));
        sky += vec3(1.0, 0.3, 0.5) * sun;

        // Horizon glow
        float hglow = exp(-abs(p.y - horizonY) * 5.0);
        sky += vec3(0.8, 0.2, 0.6) * hglow * 0.5;

    } else {
        // Lower half — perspective grid floor
        float depth = -0.5 / (p.y - horizonY + 0.001);
        float scroll = t * 3.0;

        float gridX = abs(fract(p.x * depth * 0.5) - 0.5);
        float gridZ = abs(fract(depth * 0.3 + scroll) - 0.5);

        float lineX = smoothstep(0.02, 0.0, gridX);
        float lineZ = smoothstep(0.02, 0.0, gridZ);
        float grid = max(lineX, lineZ);

        // Grid color — cyan/magenta based on depth
        vec3 gridColor = mix(
            vec3(0.0, 0.8, 1.0),  // cyan near
            vec3(0.8, 0.0, 1.0),  // magenta far
            smoothstep(1.0, 15.0, depth)
        );

        // Audio-reactive terrain (mountains as sine wave displacement)
        float terrainX = p.x * 3.0;
        float terrain = 0.0;
        terrain += sin(terrainX * 1.0 + t) * bass * 0.3;
        terrain += sin(terrainX * 2.5 + t * 1.3) * mids * 0.15;
        terrain += sin(terrainX * 5.0 + t * 2.0) * highs * 0.08;

        float terrainLine = depth * 0.05;
        float terrainMask = smoothstep(0.0, 0.02, abs(p.y - horizonY + terrain * terrainLine));

        sky = vec3(0.01, 0.0, 0.02);
        sky += gridColor * grid * (0.5 + iBeat * 0.5);
        sky += vec3(0.5, 0.0, 0.3) * (1.0 - terrainMask) * (1.0 + bass);

        // Depth fog
        float fog = exp(-depth * 0.1);
        sky *= fog;

        // Horizon glow bleeds into grid
        float hglow = exp(-(horizonY - p.y) * 3.0);
        sky += vec3(0.6, 0.1, 0.4) * hglow;
    }

    // Beat flash
    sky += vec3(0.1, 0.05, 0.15) * iBeat * 0.5;

    // CRT scanline effect (subtle)
    float scanline = 0.95 + 0.05 * sin(fragCoord.y * iResolution.y * 3.14159);
    sky *= scanline;

    fragColor = vec4(sky, 1.0);
}
