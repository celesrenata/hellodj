#version 330 core

// Varda: Liquid metal / fluid simulation look.
// Smooth organic blobs that merge and split with audio.

uniform float     iTime;
uniform vec2      iResolution;
uniform sampler2D iChannel0;
uniform float     iBeat;
uniform float     iBPM;
uniform float     iBandEnergy[7];

in vec2 fragCoord;
out vec4 fragColor;

void main() {
    vec2 uv = fragCoord * 2.0 - 1.0;
    uv.x *= iResolution.x / iResolution.y;

    float t = iTime * 0.6;
    float bass = iBandEnergy[0] + iBandEnergy[1];
    float mids = iBandEnergy[2] + iBandEnergy[3] + iBandEnergy[4];
    float highs = iBandEnergy[5] + iBandEnergy[6];

    // Metaball field — sum of 1/distance contributions
    float field = 0.0;
    vec3 colorAccum = vec3(0.0);

    // Number of blobs driven by audio energy
    int numBlobs = 7;

    for (int i = 0; i < 7; i++) {
        float fi = float(i);
        float speed = 0.8 + fi * 0.2;

        // Orbit paths influenced by different bands
        vec2 blobPos = vec2(
            sin(t * speed + fi * 1.5) * (1.0 + iBandEnergy[i] * 0.5),
            cos(t * speed * 0.7 + fi * 2.3) * (0.8 + iBandEnergy[i] * 0.4)
        );

        // Beat push — blobs expand outward
        blobPos *= 1.0 + iBeat * 0.3;

        float dist = length(uv - blobPos);
        float radius = 0.3 + iBandEnergy[i] * 0.4;

        // Metaball contribution
        float contribution = radius / (dist * dist + 0.01);
        field += contribution;

        // Per-blob color based on index
        vec3 blobColor = vec3(
            0.5 + 0.5 * sin(fi * 1.0 + t * 0.5),
            0.5 + 0.5 * sin(fi * 1.5 + t * 0.3 + 2.0),
            0.5 + 0.5 * sin(fi * 2.0 + t * 0.4 + 4.0)
        );
        colorAccum += blobColor * contribution;
    }

    // Normalize accumulated color
    colorAccum /= max(field, 0.001);

    // Threshold for the metaball surface
    float threshold = 3.0 - bass * 0.5;  // Lower threshold = larger blobs on bass
    float surface = smoothstep(threshold - 0.5, threshold + 0.5, field);

    // Surface coloring
    vec3 col = vec3(0.0);

    if (surface > 0.01) {
        // Metallic shading — fake normal from field gradient
        vec2 eps = vec2(0.01, 0.0);
        float fx = 0.0, fy = 0.0;
        // Approximate gradient for fake normal
        for (int i = 0; i < 7; i++) {
            float fi = float(i);
            float speed = 0.8 + fi * 0.2;
            vec2 bp = vec2(
                sin(t * speed + fi * 1.5) * (1.0 + iBandEnergy[i] * 0.5),
                cos(t * speed * 0.7 + fi * 2.3) * (0.8 + iBandEnergy[i] * 0.4)
            );
            bp *= 1.0 + iBeat * 0.3;
            float radius = 0.3 + iBandEnergy[i] * 0.4;
            float d1 = length(uv + eps.xy - bp);
            float d2 = length(uv - eps.xy - bp);
            float d3 = length(uv + eps.yx - bp);
            float d4 = length(uv - eps.yx - bp);
            fx += radius / (d1 * d1 + 0.01) - radius / (d2 * d2 + 0.01);
            fy += radius / (d3 * d3 + 0.01) - radius / (d4 * d4 + 0.01);
        }
        vec2 normal2D = normalize(vec2(fx, fy));

        // Specular highlight
        vec2 lightDir = normalize(vec2(0.5, 0.7));
        float spec = pow(max(dot(normal2D, lightDir), 0.0), 16.0);

        // Fresnel-like edge brightening
        float fresnel = pow(1.0 - surface, 2.0);

        col = colorAccum * surface * 0.7;
        col += vec3(0.9, 0.8, 1.0) * spec * 0.6 * (1.0 + iBeat);
        col += colorAccum * fresnel * 0.3;
    }

    // Background — subtle dark gradient
    vec3 bg = vec3(0.02, 0.01, 0.04) * (1.0 + 0.3 * (1.0 - length(uv) * 0.3));
    col = max(col, bg);

    // Beat glow
    col += vec3(0.03, 0.01, 0.05) * iBeat;

    fragColor = vec4(col, 1.0);
}
