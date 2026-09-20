// Writing the figure out: the SVG, PNGs rendered from it, and the CSV.
//
// The shell has no dialog or filesystem plugin enabled, and adding one is a
// capability change of its own, so files leave through an anchor with a
// download attribute and a Blob, which a webview and a browser both honor.
// They land in the downloads folder under the map's id.

import type { Scene } from "./scene.ts";
import { sceneToSvg } from "./scene.ts";

export function fileToDataUrl(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error ?? new Error("the image could not be read"));
    reader.readAsDataURL(file);
  });
}

/** Rasterise the scene at `pixelScale` times its nominal size. The SVG is
 *  handed over as a data URL with any photograph inlined, so the canvas is
 *  not tainted and toBlob is allowed. */
export function sceneToPng(scene: Scene, pixelScale: number): Promise<Blob> {
  const svg = sceneToSvg(scene, pixelScale);
  const width = Math.round(scene.width * pixelScale);
  const height = Math.round(scene.height * pixelScale);
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => {
      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      const context = canvas.getContext("2d");
      if (!context) return reject(new Error("no 2D canvas in this webview"));
      context.drawImage(image, 0, 0, width, height);
      canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("the PNG could not be encoded"))), "image/png");
    };
    image.onerror = () => reject(new Error("the SVG could not be rendered"));
    image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  });
}

export function download(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // The click only queues the download; give it time to read the Blob.
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

export interface ExportedFile {
  name: string;
  blob: Blob;
}

/** The files of one export, in the order they are written. */
export async function figureFiles(scene: Scene, csv: string, stem: string): Promise<ExportedFile[]> {
  const files: ExportedFile[] = [{ name: `${stem}.svg`, blob: new Blob([sceneToSvg(scene)], { type: "image/svg+xml" }) }];
  for (const scale of [2, 4]) files.push({ name: `${stem}@${scale}x.png`, blob: await sceneToPng(scene, scale) });
  files.push({ name: `${stem}.csv`, blob: new Blob([csv], { type: "text/csv" }) });
  return files;
}

/** Hand every file to the browser. No pause between them: a timer in a
 *  window that is not in front can be held back for a minute, and the
 *  export would arrive in pieces. */
export function downloadAll(files: ExportedFile[]): void {
  for (const file of files) download(file.blob, file.name);
}
