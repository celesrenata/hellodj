#version 330 core

// Cyber — Polar→tube UV wireframe tunnel with neon edge glow.
// Forward speed from iBandEnergy[0]. Edge count from iBandEnergy[3..4].
// Beat triggers geometry morph (hex→octagon→circle). Neon edge glow
// cycling cyan/magenta/yellow. Continuous scroll from iTime.
// (Req 6 AC 3, Req 7 AC 1-3, Req 8 AC 1)

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

#define PI 3.14159265359
#define TAU 6.28318530718

// --- Self-contained math utilities ---

mat2 rotate2d(float angle) {
    float c = cos(angle);
    float s = sin(angle);
    return mat2(c, -s, s, c);
}

float hash(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
}

// Polygon distance: returns distance from a point to the edge of a
// regular polygon with N sides, centered at origin with radius 1.
// Used to shape the tunnel cross-section (hex→octagon→circle morph).
float polygonDist(vec2 p, float n) {
    float angle = atan(p.y, p.x);
    float sector = TAU / n;
    // Angle within current sector
    float a = mod(angle + sector * 0.5, sector) - sector * 0.5;
    // Distance to polygon edge at this angle
    return length(p) * cos(a);
}

void main() {
    // Center UV with aspect correction
    vec2 uv = vUV - 0.5;
    float aspect = iResolution.x / iResolution.y;
    uv.x *= aspect;

    // --- Tunnel parameters ---

    // Forward speed: base + iBandEnergy[0] boost (Req 7 AC 3 — band 0)
    // Continuous scroll from iTime ensures animation without audio (Req 8 AC 1)
    float baseSpeed = 0.4;
    float speed = baseSpeed + iBandEnergy[0] * 1.2;

    // Edge count from iBandEnergy[3] + iBandEnergy[4] (Req 7 AC 3 — bands 3,4)
    // Range: 6 (hex) to 16 (near-circle)
    float edgeEnergy = iBandEnergy[3] * 0.6 + iBandEnergy[4] * 0.4;
    float baseEdges = 6.0;
    float edgeCount = baseEdges + edgeEnergy * 10.0;

    // --- Beat-triggered geometry morph (Req 7 AC 1 — structural change) ---
    // Morph cross-section: hex (6) → octagon (8) → circle (high N)
    // When beat fires, snap edge count toward circle then decay back
    float morphTarget = 6.0 + iBeat * 18.0; // iBeat=1 → 24 edges (circle-like)
    edgeCount = max(edgeCount, morphTarget);
    edgeCount = clamp(edgeCount, 6.0, 32.0);

    // --- Polar → tube UV mapping ---
    float angle = atan(uv.y, uv.x); // [-PI, PI]
    float radius = length(uv);

    // Avoid division by zero at exact center
    float depth = 1.0 / max(radius, 0.001);

    // Tube UV: angle wraps around, depth scrolls forward
    vec2 tubeUV = vec2(angle / TAU + 0.5, depth);

    // Apply forward motion (continuous from iTime + audio-reactive speed)
    tubeUV.y += iTime * speed;

    // --- Wireframe grid on tunnel surface ---

    // Longitudinal lines (edges of the polygon cross-section)
    float longLines = edgeCount;
    float angularCell = fract(tubeUV.x * longLines);
    float longDist = abs(angularCell - 0.5);

    // Lateral rings (depth rings along tunnel)
    float ringFreq = 4.0; // rings per unit depth
    float ringCell = fract(tubeUV.y * ringFreq);
    float ringDist = abs(ringCell - 0.5);

    // Line thickness varies with depth (thinner far away for perspective)
    float depthFade = 1.0 / (1.0 + depth * 0.05);
    float lineThickness = 0.04 + 0.02 * depthFade;

    // Wireframe edges
    float longLine = smoothstep(lineThickness, 0.0, longDist);
    float ringLine = smoothstep(lineThickness, 0.0, ringDist);
    float wireframe = max(longLine, ringLine);

    // --- Polygon cross-section shaping ---
    // Distort radius by polygon shape to make tunnel appear polygonal
    float polyDist = polygonDist(uv, edgeCount);
    // Edge proximity creates brighter edges at polygon corners
    float polyEdge = 1.0 - smoothstep(0.0, 0.02, abs(fract(angle / TAU * edgeCount) - 0.5));

    // --- Neon edge coloring: cycle through cyan/magenta/yellow ---
    float colorCycle = iTime * 0.5 + tubeUV.y * 0.3;
    float colorPhase = fract(colorCycle / 3.0) * 3.0;

    vec3 cyan    = vec3(0.0, 0.9, 1.0);
    vec3 magenta = vec3(1.0, 0.1, 0.8);
    vec3 yellow  = vec3(1.0, 0.9, 0.1);

    vec3 neonColor;
    if (colorPhase < 1.0) {
        neonColor = mix(cyan, magenta, colorPhase);
    } else if (colorPhase < 2.0) {
        neonColor = mix(magenta, yellow, colorPhase - 1.0);
    } else {
        neonColor = mix(yellow, cyan, colorPhase - 2.0);
    }

    // Vary color slightly along angular position for visual interest
    float angularVariation = sin(angle * 3.0 + iTime) * 0.15;
    neonColor += angularVariation;

    // --- Glow effect ---
    // Neon glow on wireframe edges (exponential falloff from line center)
    float glowLong = exp(-longDist * 20.0) * 0.7;
    float glowRing = exp(-ringDist * 20.0) * 0.7;
    float glow = max(glowLong, glowRing);

    // Combine wireframe + glow
    float edgeIntensity = wireframe + glow * 0.6;

    // --- Depth fog (darken distant parts of tunnel) ---
    float fog = 1.0 - exp(-radius * 3.0);
    fog = clamp(fog, 0.2, 1.0);

    // Apply depth-based brightness falloff
    float brightness = edgeIntensity * fog;

    // --- Compose color ---
    vec3 col = neonColor * brightness;

    // Add subtle polygon edge highlight at the tunnel mouth
    float mouthGlow = exp(-abs(radius - 0.3) * 10.0) * polyEdge * 0.4;
    col += neonColor * mouthGlow;

    // Dark background (tunnel interior)
    vec3 bgColor = vec3(0.01, 0.005, 0.03);
    col = mix(bgColor, col, clamp(edgeIntensity + glow * 0.3, 0.0, 1.0));

    // --- FFT texture: add reactive ripples along rings ---
    float fftSample = texture(iFFT, fract(tubeUV.x)).r;
    col += neonColor * fftSample * 0.15 * fog;

    // --- Peak state (Req 7 AC 2): while iBeat > 0.5, 2+ parameters differ ---
    // Parameter 1: speed already boosted (morphTarget pushes edge count)
    // Parameter 2: overall brightness surge + color flash
    float peakFactor = smoothstep(0.5, 1.0, iBeat);
    col *= 1.0 + peakFactor * 1.5;
    // Parameter 3: white flash at tunnel center
    float centerFlash = exp(-radius * 8.0) * peakFactor * 2.0;
    col += vec3(1.0, 0.95, 0.9) * centerFlash;

    // --- iBandEnergy[6] (brilliance): edge glow sparkle intensity ---
    col += neonColor * wireframe * iBandEnergy[6] * 0.4;

    // Glow intensity uniform modulation
    col *= 0.7 + iGlowIntensity * 0.3;

    // Background opacity
    col *= iBgOpacity;

    // Band usage summary (Req 7 AC 3 — minimum 3 distinct bands):
    // iBandEnergy[0] — forward tunnel speed
    // iBandEnergy[3] — edge count (primary)
    // iBandEnergy[4] — edge count (secondary)
    // iBandEnergy[6] — edge glow sparkle (bonus 4th band)

    fragColor = vec4(col, 1.0);
}
