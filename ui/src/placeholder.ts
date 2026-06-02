const DEFAULT_PLACEHOLDER = "TypeScript dashboard shell";

export function placeholderText(label: string = DEFAULT_PLACEHOLDER): string {
  const normalized = label.trim();
  return normalized.length > 0 ? normalized : DEFAULT_PLACEHOLDER;
}
