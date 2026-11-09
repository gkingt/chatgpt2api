import type { ImageOutputOptions } from "@/lib/api";

type ImageMetadata = { mime_type?: string; output_format?: string; url?: string };
const mimeTypes: Record<string, string> = { png: "image/png", jpeg: "image/jpeg", jpg: "image/jpeg", webp: "image/webp", gif: "image/gif", svg: "image/svg+xml" };

export function imageExtension(image: ImageMetadata): string {
  const extension = image.output_format || Object.keys(mimeTypes).find((key) => mimeTypes[key] === image.mime_type)
    || image.url?.split(/[?#]/)[0].match(/\.(png|jpe?g|webp|gif|svg)$/i)?.[1] || "png";
  return extension === "jpeg" ? "jpg" : extension.toLowerCase();
}

export function imageMimeType(image: ImageMetadata): string {
  return image.mime_type || mimeTypes[imageExtension(image)] || "image/png";
}

export function imageOptionsForModel(model: string, options: ImageOutputOptions): ImageOutputOptions {
  if (!model.includes("2.6-")) return { output_format: "png" };
  if (model.includes("codex")) return options;
  return options.output_format === "gif"
    ? { output_format: "gif", frame_count: options.frame_count ?? 9, frame_duration_ms: options.frame_duration_ms ?? 200 }
    : { output_format: "png" };
}
