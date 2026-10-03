/**
 * Defect #4 — the browser-side square crop (avatar proposal D2:
 * "frontend crops to square before upload"). Thin canvas wrapper over
 * the unit-pinned math in avatarView (squareCropRect /
 * avatarDisplaySize); no image library, no new dependency.
 */
import {
  avatarDisplaySize,
  squareCropRect,
} from "./avatarView";

/**
 * Decode the picked image, center-crop it to a square, downscale to
 * the display target, and re-encode as the SAME type. Rejects files
 * the browser cannot decode (the server's magic-number check remains
 * the authority either way).
 */
export async function cropToSquareFile(file: File): Promise<File> {
  const bitmap = await createImageBitmap(file);
  try {
    const { sx, sy, size } = squareCropRect(bitmap.width, bitmap.height);
    const target = avatarDisplaySize(size);
    const canvas = document.createElement("canvas");
    canvas.width = target;
    canvas.height = target;
    const context = canvas.getContext("2d");
    if (context === null) {
      throw new Error("canvas 2d context unavailable");
    }
    context.drawImage(bitmap, sx, sy, size, size, 0, 0, target, target);
    const blob = await new Promise<Blob | null>((resolve) => {
      canvas.toBlob(resolve, file.type);
    });
    if (blob === null) {
      throw new Error("avatar re-encode failed");
    }
    return new File([blob], file.name, { type: file.type });
  } finally {
    bitmap.close();
  }
}
