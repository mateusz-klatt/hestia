// Shared decorative category swatch and AA-contrast category badge components.
import { categoryBadgeStyle, categorySwatchStyle } from "./category_colors";
import type { CategoryColor } from "./category_colors";

export function CategorySwatch({ color, className = "budget-cat-swatch" }: {
  color: Pick<CategoryColor, "background" | "hatched">;
  className?: string;
}) {
  return (
    <span
      className={className}
      style={categorySwatchStyle(color)}
      data-hatched={color.hatched ? "true" : undefined}
      aria-hidden="true"
    />
  );
}

export function CategoryBadge({ color, label, className = "budget-chip budget-cat-badge" }: {
  color: Pick<CategoryColor, "background" | "text" | "oklch" | "hatched">;
  label: string;
  className?: string;
}) {
  return (
    <span
      className={className}
      style={categoryBadgeStyle(color)}
      data-hatched={color.hatched ? "true" : undefined}
    >
      {label}
    </span>
  );
}
