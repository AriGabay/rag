/** Copies `text` to the clipboard. False when the browser refuses (no permission, or no clipboard API). */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
