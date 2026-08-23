#version 330 core

// Neural-network-like connections reacting to frequency bands.
// Shadertoy-compatible uniform convention.

uniform float     iTime;
uniform vec2      iResolution;
uniform sampler2D iChannel0;    // 512x2 audio texture (row 0: waveform, row 1: FFT)
uniform float     iBeat;        // 0-1 decaying beat pulse
uniform float     iBPM;
uniform float     iBandEnergy[7];

in vec2 fragCoord;
out vec4 fragColor;

float hash1(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
}

vec2 nodePos(float id) {
    float t = iTime * 0.3;
    return vec2(
        sin(t * 0.7 + id * 3.14) * 0.6 + sin(id * 5.0) * 0.3,
        cos(t * 0.5 + id * 2.7) * 0.6 + cos(id * 4.0) * 0.3
    );
}

void main() {
    vec2 uv = fragCoord;
    vec2 p = uv * 2.0 - 1.0;
    p.x *= iResolution.x / iResolution.y;

    float bass = iBandEnergy[0] + iBandEnergy[1];
    float mid = iBandEnergy[2] + iBandEnergy[3];
    float high = iBandEnergy[5] + iBandEnergy[6];

    vec3 color = vec3(0.01, 0.005, 0.02); // dark background
    float totalEnergy = bass + mid + high;

    int numNodes = 12;

    // Draw connections between nodes
    for (int i = 0; i < 12; i++) {
        vec2 nodeA = nodePos(float(i));

        for (int j = i + 1; j < 12; j++) {
            vec2 nodeB = nodePos(float(j));

            // Band-dependent connection strength
            int bandIdx = (i + j) % 7;
            float energy = iBandEnergy[bandIdx];

            // Only draw connection if energy threshold met
            if (energy < 0.1) continue;

            // Distance from point to line segment
            vec2 ab = nodeB - nodeA;
            float t_proj = clamp(dot(p - nodeA, ab) / dot(ab, ab), 0.0, 1.0);
            vec2 closest = nodeA + ab * t_proj;
            float dist = length(p - closest);

            // Connection glow (thinner = weaker connection)
            float thickness = 0.003 + energy * 0.01;
            float glow = thickness / (dist + thickness);
            glow *= energy;

            // Color based on band
            float hue = float(bandIdx) / 7.0 * 6.28 + iTime * 0.5;
            vec3 lineColor = 0.5 + 0.5 * cos(hue + vec3(0.0, 2.094, 4.189));

            // Pulse along connection
            float pulse = sin(t_proj * 12.0 - iTime * 4.0) * 0.5 + 0.5;
            glow *= 0.5 + pulse * 0.5;

            color += lineColor * glow * 0.3;
        }

        // Draw node glow
        float nodeDist = length(p - nodeA);
        float nodeGlow = 0.008 / (nodeDist * nodeDist + 0.001);

        // Node brightness from associated band
        int nodeBand = i % 7;
        float nodeEnergy = iBandEnergy[nodeBand];
        nodeGlow *= 0.5 + nodeEnergy;

        // Beat flash on nodes
        nodeGlow *= 1.0 + iBeat * 2.0;

        float nodeHue = float(nodeBand) / 7.0 * 6.28 + iTime * 0.3;
        vec3 nodeColor = 0.5 + 0.5 * cos(nodeHue + vec3(0.0, 2.094, 4.189));
        color += nodeColor * nodeGlow;
    }

    // Subtle overall brightness from total energy
    color *= 0.8 + totalEnergy * 0.2;

    fragColor = vec4(color, 1.0);
}
