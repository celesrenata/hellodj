/**
 * Canvas resize handling — anchors the whiteboard canvas to the video's
 * actual rendered content area (accounting for object-fit: contain letterboxing).
 *
 * Watches both the video container and the video element's intrinsic dimensions
 * to compute the exact pixel rect where video content is displayed, then
 * positions and sizes the canvas to match.
 *
 * @module canvas_resize
 * @see WhiteboardOverlay#resize
 * @requirement 13.3
 */

/**
 * Compute the actual rendered rectangle of a video element using object-fit: contain.
 * Returns the position and size of the video content within its container.
 *
 * @param {HTMLVideoElement} video - The video element
 * @returns {{ x: number, y: number, width: number, height: number } | null}
 */
export function getVideoContentRect(video) {
  const containerRect = video.getBoundingClientRect();
  const containerW = containerRect.width;
  const containerH = containerRect.height;

  const videoW = video.videoWidth;
  const videoH = video.videoHeight;

  // If video has no intrinsic dimensions yet, fall back to full container
  if (!videoW || !videoH || containerW === 0 || containerH === 0) {
    return { x: 0, y: 0, width: containerW, height: containerH };
  }

  // object-fit: contain — fit video inside container preserving aspect ratio
  const containerAspect = containerW / containerH;
  const videoAspect = videoW / videoH;

  let renderW, renderH, offsetX, offsetY;

  if (videoAspect > containerAspect) {
    // Video is wider than container — letterbox top/bottom
    renderW = containerW;
    renderH = containerW / videoAspect;
    offsetX = 0;
    offsetY = (containerH - renderH) / 2;
  } else {
    // Video is taller than container — pillarbox left/right
    renderH = containerH;
    renderW = containerH * videoAspect;
    offsetX = (containerW - renderW) / 2;
    offsetY = 0;
  }

  return {
    x: Math.round(offsetX),
    y: Math.round(offsetY),
    width: Math.round(renderW),
    height: Math.round(renderH),
  };
}

/**
 * Initialize automatic canvas resize handling anchored to video geometry.
 *
 * Creates a ResizeObserver on the canvas's parent element and listens
 * for video metadata/resize events. On each change, computes the video's
 * rendered content rect and calls overlay.resize() with those dimensions.
 *
 * Performs an initial resize call immediately to set correct dimensions.
 *
 * @param {HTMLCanvasElement} canvas - The whiteboard canvas element
 * @param {import('./whiteboard.js').WhiteboardOverlay} overlay - The overlay instance with a resize() method
 * @returns {{ disconnect: () => void }} Cleanup handle to stop observing
 */
export function initCanvasResize(canvas, overlay) {
  const parent = canvas.parentElement;
  const video = parent.querySelector('video');

  const doResize = () => {
    if (!video) {
      // No video element — fall back to parent dimensions
      overlay.resize();
      return;
    }

    const rect = getVideoContentRect(video);
    if (!rect || rect.width === 0 || rect.height === 0) return;

    overlay.resize(rect);
  };

  // Observe container size changes
  const observer = new ResizeObserver(() => {
    doResize();
  });
  observer.observe(parent);

  // Listen for video dimension changes (metadata loaded, resolution switch)
  if (video) {
    video.addEventListener('loadedmetadata', doResize);
    video.addEventListener('resize', doResize); // fired when videoWidth/Height changes
  }

  // Perform initial resize
  doResize();

  return {
    disconnect() {
      observer.disconnect();
      if (video) {
        video.removeEventListener('loadedmetadata', doResize);
        video.removeEventListener('resize', doResize);
      }
    },
  };
}
