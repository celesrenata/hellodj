/**
 * Coordinate normalization utilities for the whiteboard overlay.
 *
 * All stroke data uses viewport-relative coordinates (0.0–1.0) so
 * drawings render at correct positions regardless of viewer screen size.
 * Normalization uses 4 decimal places (0.01% precision).
 */

/**
 * Normalize pixel coordinates to the 0.0–1.0 range.
 * Out-of-bounds values are clamped.
 *
 * @param {number} pixelX - Horizontal pixel position
 * @param {number} pixelY - Vertical pixel position
 * @param {number} width  - Viewport width in pixels
 * @param {number} height - Viewport height in pixels
 * @returns {[number, number]} Normalized [x, y] each in [0, 1]
 */
export function normalize(pixelX, pixelY, width, height) {
  const x = Math.round(Math.max(0, Math.min(1, pixelX / width)) * 10000) / 10000;
  const y = Math.round(Math.max(0, Math.min(1, pixelY / height)) * 10000) / 10000;
  return [x, y];
}

/**
 * Denormalize coordinates back to pixel positions.
 *
 * @param {number} normX  - Normalized x coordinate (0.0–1.0)
 * @param {number} normY  - Normalized y coordinate (0.0–1.0)
 * @param {number} width  - Viewport width in pixels
 * @param {number} height - Viewport height in pixels
 * @returns {[number, number]} Pixel [x, y] positions
 */
export function denormalize(normX, normY, width, height) {
  return [normX * width, normY * height];
}

/**
 * Normalize stroke width relative to viewport width.
 *
 * @param {number} cssPixels    - Stroke width in CSS pixels
 * @param {number} viewportWidth - Viewport width in pixels
 * @returns {number} Normalized width (4 decimal places)
 */
export function normalizeWidth(cssPixels, viewportWidth) {
  return Math.round((cssPixels / viewportWidth) * 10000) / 10000;
}

/**
 * Denormalize stroke width back to CSS pixels.
 *
 * @param {number} normalizedWidth - Normalized width value
 * @param {number} viewportWidth   - Viewport width in pixels
 * @returns {number} Width in CSS pixels
 */
export function denormalizeWidth(normalizedWidth, viewportWidth) {
  return normalizedWidth * viewportWidth;
}
