import { useTheme, type Theme } from "../theme/theme";

const OPTIONS: [Theme, string][] = [
  ["light", "Light"],
  ["dark", "Dark"],
  ["system", "Auto"],
];

export function ThemeSwitch({ variant = "page" }: { variant?: "page" | "chrome" }) {
  const [theme, setTheme] = useTheme();
  return (
    <div role="group" aria-label="Appearance" className={`segmented ${variant === "chrome" ? "segmented-chrome" : ""}`}>
      {OPTIONS.map(([value, label]) => (
        <button key={value} type="button" className="op-press" aria-pressed={theme === value} onClick={() => setTheme(value)}>
          {label}
        </button>
      ))}
    </div>
  );
}
