#version 330 core

// Varda: Animated voronoi cells that pulse and shatter with beat.
// Glowing cell edges with audio-reactive color shifting.

uniform float     iTime;
uniform vec2      iResolution;
uniform sampler2D iChannel0;
uniform float     iBeat;
uniform float     iBPM;
uniform float     iBandEnergy[7];

in vec2 fragCoord;
out vec4 fragColor;

vec2 hash2(vec2 p) {
    p = vec2(dot(p, vec2(127.1, 311.7)), dot(p, vec2(269.5, 183.3)));
    return fract(sin(p) * 43758.5453) * 2.0 - 1.0;
}

void main() {
    vec2 uv = fragCoord;
    uv.x *= iResolution.x / iResolution.y;

    float t = iTime * 0.6;
    float bass = iBandEnergy[0] + iBandEnergy[1];
    float mids = iBandEnergy[2] + iBandEnergy[3] + iBandEnergy[4];

    // Scale the voronoi grid — beat makes cells larger (zoom in)
    float cellScale = 4.0 - iBeat * 0.8;
    vec2 p = uv * cellScale;

    // Voronoi computation
    vec2 cell = floor(p);
    vec2 local = fract(p);

    float minDist = 10.0;
    float secondDist = 10.0;
    vec2 nearestPoint = vec2(0.0);

    for (int j = -1; j <= 1; j++) {
        for (int i = -1; i <= 1; i++) {
            vec2 neighbor = vec2(float(i), float(j));
            vec2 offset = hash2(cell + neighbor);

            // Animate points with audio
            offset = 0.5 + 0.5 * sin(t + offset * 6.28 + bass * 2.0);

            vec2 diff = neighbor + offset - local;
            float dist = length(diff);

            if (dist < minDist) {
                secondDist = minDist;
                minDist = dist;
                nearestPoint = cell + neighbor + offset;
            } else if (dist < secondDist) {
                secondDist = dist;
            }
        }
    }

    // Edge detection (distance between closest and second-closest)
    float edge = secondDist - minDist;

    // Sharper edges on beat
    float edgeWidth = 0.08 - iBeat * 0.04;
    float edgeGlow = smoothstep(edgeWidth, 0.0, edge);

    // Cell interior color — based on cell position
    float cellId = fract(sin(dot(floor(nearestPoint), vec2(12.9898, 78.233))) * 43758.5453);
    float hue = cellId + t * 0.1 + bass * 0.2;

    // HSV-like color from hue
    vec3 cellColor = vec3(
        0.5 + 0.5 * cos(hue * 6.28 + 0.0),
        0.5 + 0.5 * cos(hue * 6.28 + 2.094),
        0.5 + 0.5 * cos(hue * 6.28 + 4.189)
    );

    // Darken cell interiors, brighten edges
    vec3 interior = cellColor * 0.15 * (1.0 + mids * 0.5);
    vec3 edgeColor = vec3(0.9, 0.7, 1.0) * (1.0 + iBeat * 2.0);

    vec3 col = mix(interior, edgeColor, edgeGlow);

    // Add glow around edges (wider, softer)
    float outerGlow = smoothstep(0.2, 0.0, edge) * 0.3;
    col += cellColor * outerGlow * (1.0 + bass);

    // Beat pulse — brighten everything
    col *= 1.0 + iBeat * 0.4;

    // Subtle vignette
    float vig = 1.0 - length(fragCoord - 0.5) * 0.5;
    col *= vig;

    fragColor = vec4(col, 1.0);
}
