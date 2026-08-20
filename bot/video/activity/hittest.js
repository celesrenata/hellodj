/**
 * Hit-testing module for the whiteboard overlay.
 *
 * All coordinates are normalized (0.0–1.0). The tolerance is provided
 * in CSS pixels and normalized against the viewport dimensions.
 */

/**
 * Compute perpendicular distance from point (px, py) to line segment (ax, ay)→(bx, by).
 *
 * @param {number} px - Point x
 * @param {number} py - Point y
 * @param {number} ax - Segment start x
 * @param {number} ay - Segment start y
 * @param {number} bx - Segment end x
 * @param {number} by - Segment end y
 * @returns {number} Distance from point to nearest point on segment
 */
export function distToSegment(px, py, ax, ay, bx, by) {
  const dx = bx - ax;
  const dy = by - ay;
  const lenSq = dx * dx + dy * dy;

  if (lenSq === 0) {
    // Segment is a single point
    const ex = px - ax;
    const ey = py - ay;
    return Math.sqrt(ex * ex + ey * ey);
  }

  // Parameter t of the projection onto the line, clamped to [0, 1]
  let t = ((px - ax) * dx + (py - ay) * dy) / lenSq;
  t = Math.max(0, Math.min(1, t));

  const projX = ax + t * dx;
  const projY = ay + t * dy;
  const ex = px - projX;
  const ey = py - projY;
  return Math.sqrt(ex * ex + ey * ey);
}

/**
 * Hit-test a click point against a freehand stroke.
 * Returns the minimum distance from the click to any line segment
 * between consecutive points.
 *
 * @param {number} cx - Click x (normalized)
 * @param {number} cy - Click y (normalized)
 * @param {Array<[number, number]>} points - Stroke points
 * @returns {number} Minimum distance
 */
function hitFreehand(cx, cy, points) {
  if (points.length < 2) {
    if (points.length === 1) {
      const dx = cx - points[0][0];
      const dy = cy - points[0][1];
      return Math.sqrt(dx * dx + dy * dy);
    }
    return Infinity;
  }

  let minDist = Infinity;
  for (let i = 0; i < points.length - 1; i++) {
    const d = distToSegment(
      cx, cy,
      points[i][0], points[i][1],
      points[i + 1][0], points[i + 1][1]
    );
    if (d < minDist) minDist = d;
  }
  return minDist;
}

/**
 * Hit-test a click point against a line or arrow stroke.
 * Distance to the single line segment defined by the two points.
 *
 * @param {number} cx - Click x (normalized)
 * @param {number} cy - Click y (normalized)
 * @param {Array<[number, number]>} points - Exactly 2 points [start, end]
 * @returns {number} Distance to the line segment
 */
function hitLine(cx, cy, points) {
  if (points.length < 2) return Infinity;
  return distToSegment(
    cx, cy,
    points[0][0], points[0][1],
    points[1][0], points[1][1]
  );
}

/**
 * Hit-test a click point against a rectangle stroke.
 * Distance to the nearest of the 4 edge segments.
 *
 * @param {number} cx - Click x (normalized)
 * @param {number} cy - Click y (normalized)
 * @param {Array<[number, number]>} points - Exactly 2 points [topLeft, bottomRight]
 * @returns {number} Minimum distance to any edge
 */
function hitRect(cx, cy, points) {
  if (points.length < 2) return Infinity;

  const [x1, y1] = points[0];
  const [x2, y2] = points[1];

  // Four corners of the rectangle
  const tl = [Math.min(x1, x2), Math.min(y1, y2)];
  const tr = [Math.max(x1, x2), Math.min(y1, y2)];
  const br = [Math.max(x1, x2), Math.max(y1, y2)];
  const bl = [Math.min(x1, x2), Math.max(y1, y2)];

  // Distance to each of the 4 edges
  const d1 = distToSegment(cx, cy, tl[0], tl[1], tr[0], tr[1]); // top
  const d2 = distToSegment(cx, cy, tr[0], tr[1], br[0], br[1]); // right
  const d3 = distToSegment(cx, cy, br[0], br[1], bl[0], bl[1]); // bottom
  const d4 = distToSegment(cx, cy, bl[0], bl[1], tl[0], tl[1]); // left

  return Math.min(d1, d2, d3, d4);
}

/**
 * Hit-test a click point against an ellipse stroke.
 * Samples ~32 points on the ellipse perimeter and tests distance
 * to segments between consecutive samples.
 *
 * @param {number} cx - Click x (normalized)
 * @param {number} cy - Click y (normalized)
 * @param {Array<[number, number]>} points - Exactly 2 points [topLeft, bottomRight] of bounding box
 * @returns {number} Minimum distance to sampled perimeter
 */
function hitEllipse(cx, cy, points) {
  if (points.length < 2) return Infinity;

  const [x1, y1] = points[0];
  const [x2, y2] = points[1];

  // Ellipse center and radii
  const centerX = (x1 + x2) / 2;
  const centerY = (y1 + y2) / 2;
  const rx = Math.abs(x2 - x1) / 2;
  const ry = Math.abs(y2 - y1) / 2;

  if (rx === 0 && ry === 0) {
    const dx = cx - centerX;
    const dy = cy - centerY;
    return Math.sqrt(dx * dx + dy * dy);
  }

  const NUM_SAMPLES = 32;
  let minDist = Infinity;

  // Generate sample points on the ellipse perimeter
  for (let i = 0; i < NUM_SAMPLES; i++) {
    const angle1 = (2 * Math.PI * i) / NUM_SAMPLES;
    const angle2 = (2 * Math.PI * ((i + 1) % NUM_SAMPLES)) / NUM_SAMPLES;

    const ax = centerX + rx * Math.cos(angle1);
    const ay = centerY + ry * Math.sin(angle1);
    const bx = centerX + rx * Math.cos(angle2);
    const by = centerY + ry * Math.sin(angle2);

    const d = distToSegment(cx, cy, ax, ay, bx, by);
    if (d < minDist) minDist = d;
  }

  return minDist;
}

/**
 * Hit-test a click point against a text stroke using bounding box containment.
 * Text has a single anchor point — we approximate a bounding box around it.
 * Since text strokes have exactly 1 point (the position), we use a fixed
 * approximate size for the bounding box (based on typical text dimensions).
 *
 * For simplicity, we treat text as a small bounding box around the anchor point.
 * The box size is roughly 200px wide × 20px tall normalized to the viewport.
 * However, since we don't know exact rendered size in normalized space,
 * we use the tolerance as the hit area around the text anchor.
 *
 * @param {number} cx - Click x (normalized)
 * @param {number} cy - Click y (normalized)
 * @param {Array<[number, number]>} points - Exactly 1 point [position]
 * @param {number} tolerance - Normalized tolerance value
 * @returns {boolean} True if the click is within the text bounding box
 */
function hitTextBbox(cx, cy, points, tolerance) {
  if (points.length < 1) return false;

  // Text has a single point (anchor). Use a generous bounding box
  // approximation: tolerance * 10 wide, tolerance * 3 tall from the anchor.
  // This is a rough heuristic since we don't have rendered dimensions.
  const [x, y] = points[0];
  const boxWidth = tolerance * 10;
  const boxHeight = tolerance * 3;

  return cx >= x && cx <= x + boxWidth && cy >= y - boxHeight && cy <= y + boxHeight;
}

/**
 * Hit-test a click point against a sticker or rect-like bounding box.
 * Uses simple point-in-rectangle containment.
 *
 * @param {number} cx - Click x (normalized)
 * @param {number} cy - Click y (normalized)
 * @param {Array<[number, number]>} points - Exactly 2 points [topLeft, bottomRight]
 * @returns {boolean} True if the click is inside the bounding box
 */
function hitBbox(cx, cy, points) {
  if (points.length < 2) return false;

  const [x1, y1] = points[0];
  const [x2, y2] = points[1];

  const minX = Math.min(x1, x2);
  const maxX = Math.max(x1, x2);
  const minY = Math.min(y1, y2);
  const maxY = Math.max(y1, y2);

  return cx >= minX && cx <= maxX && cy >= minY && cy <= maxY;
}

/**
 * Perform hit-testing against all strokes to find the topmost stroke
 * at the given click position within the specified tolerance.
 *
 * Iterates strokes in reverse insertion order (topmost/most recent first).
 *
 * @param {number} clickX - Click x coordinate (normalized 0.0–1.0)
 * @param {number} clickY - Click y coordinate (normalized 0.0–1.0)
 * @param {Array<object>} strokes - Array of stroke objects in insertion order
 * @param {number} tolerancePx - Tolerance in CSS pixels
 * @param {number} viewportWidth - Viewport width in CSS pixels
 * @param {number} viewportHeight - Viewport height in CSS pixels
 * @returns {object|null} The topmost hit stroke, or null if none hit
 */
export function hitTest(clickX, clickY, strokes, tolerancePx, viewportWidth, viewportHeight) {
  if (!strokes || strokes.length === 0) return null;

  // Normalize tolerance to viewport dimensions
  const tolX = tolerancePx / viewportWidth;
  const tolY = tolerancePx / viewportHeight;
  // Use the maximum of tolX/tolY since aspect ratio may differ
  const tolerance = Math.max(tolX, tolY);

  // Iterate in reverse (topmost stroke first)
  for (let i = strokes.length - 1; i >= 0; i--) {
    const stroke = strokes[i];
    const { type, points } = stroke;

    if (!points || points.length === 0) continue;

    let hit = false;

    switch (type) {
      case 'freehand': {
        const dist = hitFreehand(clickX, clickY, points);
        hit = dist <= tolerance;
        break;
      }
      case 'line':
      case 'arrow': {
        const dist = hitLine(clickX, clickY, points);
        hit = dist <= tolerance;
        break;
      }
      case 'rect': {
        const dist = hitRect(clickX, clickY, points);
        hit = dist <= tolerance;
        break;
      }
      case 'ellipse': {
        const dist = hitEllipse(clickX, clickY, points);
        hit = dist <= tolerance;
        break;
      }
      case 'text': {
        hit = hitTextBbox(clickX, clickY, points, tolerance);
        break;
      }
      case 'sticker': {
        hit = hitBbox(clickX, clickY, points);
        break;
      }
      default:
        break;
    }

    if (hit) return stroke;
  }

  return null;
}
