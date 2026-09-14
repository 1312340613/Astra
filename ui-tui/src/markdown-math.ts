export interface TerminalMath {
  text: string;
  malformed: boolean;
}

const SYMBOLS: Record<string, string> = {
  Rightarrow: "⇒",
  Leftarrow: "⇐",
  Leftrightarrow: "⇔",
  rightarrow: "→",
  leftarrow: "←",
  times: "×",
  cdot: "·",
  neq: "≠",
  ne: "≠",
  geq: "≥",
  leq: "≤",
  approx: "≈",
  gg: "≫",
  ll: "≪",
  sum: "∑",
  prod: "∏",
  infty: "∞",
  pm: "±",
  div: "÷",
  alpha: "α",
  beta: "β",
  gamma: "γ",
  delta: "δ",
  theta: "θ",
  lambda: "λ",
  mu: "μ",
  pi: "π",
  sigma: "σ",
  phi: "φ",
  omega: "ω",
};

const SUPERSCRIPT: Record<string, string> = {
  "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
  "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
  "+": "⁺", "-": "⁻", "=": "⁼", "(": "⁽", ")": "⁾",
  n: "ⁿ", i: "ⁱ",
};

const SUBSCRIPT: Record<string, string> = {
  "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄",
  "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉",
  "+": "₊", "-": "₋", "=": "₌", "(": "₍", ")": "₎",
  a: "ₐ", e: "ₑ", h: "ₕ", i: "ᵢ", j: "ⱼ", k: "ₖ", l: "ₗ",
  m: "ₘ", n: "ₙ", o: "ₒ", p: "ₚ", r: "ᵣ", s: "ₛ", t: "ₜ",
  u: "ᵤ", v: "ᵥ", x: "ₓ",
};

interface BracedValue {
  value: string;
  end: number;
}

function readBraced(source: string, from: number): BracedValue | null {
  let start = from;
  while (source[start] === " " || source[start] === "\t") start += 1;
  if (source[start] !== "{") return null;

  let depth = 0;
  for (let i = start; i < source.length; i += 1) {
    if (source[i] === "\\") {
      i += 1;
      continue;
    }
    if (source[i] === "{") depth += 1;
    if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) return { value: source.slice(start + 1, i), end: i + 1 };
    }
  }
  return null;
}

function scriptText(value: string, table: Record<string, string>, fallback: "_" | "^"): string {
  const converted = [...value].map((char) => table[char]).join("");
  return converted.length === value.length ? converted : `${fallback}(${value})`;
}

function convertCore(source: string, depth = 0): string {
  if (depth > 12) return source;
  let output = "";

  for (let i = 0; i < source.length;) {
    const char = source[i];
    if (char === "\\") {
      const commandMatch = source.slice(i + 1).match(/^[A-Za-z]+/);
      if (!commandMatch) {
        output += source[i + 1] ?? "";
        i += 2;
        continue;
      }
      const command = commandMatch[0];
      let next = i + command.length + 1;

      if (command === "left" || command === "right") {
        i = next;
        continue;
      }
      if (command === "frac") {
        const numerator = readBraced(source, next);
        const denominator = numerator ? readBraced(source, numerator.end) : null;
        if (numerator && denominator) {
          output += `(${convertCore(numerator.value, depth + 1)})/(${convertCore(denominator.value, depth + 1)})`;
          i = denominator.end;
          continue;
        }
      }
      if (command === "sqrt") {
        const value = readBraced(source, next);
        if (value) {
          output += `√(${convertCore(value.value, depth + 1)})`;
          i = value.end;
          continue;
        }
      }
      if (["text", "mathrm", "mathbf", "mathit", "operatorname"].includes(command)) {
        const value = readBraced(source, next);
        if (value) {
          output += convertCore(value.value, depth + 1);
          i = value.end;
          continue;
        }
      }
      if (command === "boxed") {
        const value = readBraced(source, next);
        if (value) {
          output += `⟦${convertCore(value.value, depth + 1)}⟧`;
          i = value.end;
          continue;
        }
      }
      if (SYMBOLS[command]) {
        output += SYMBOLS[command];
        i = next;
        continue;
      }
      if ([",", ";", ":", "!", "quad", "qquad"].includes(command)) {
        output += " ";
        i = next;
        continue;
      }

      output += `\\${command}`;
      i = next;
      continue;
    }

    if (char === "_" || char === "^") {
      const braced = readBraced(source, i + 1);
      if (braced) {
        const value = convertCore(braced.value, depth + 1);
        output += scriptText(value, char === "_" ? SUBSCRIPT : SUPERSCRIPT, char);
        i = braced.end;
        continue;
      }
      const value = source[i + 1];
      if (value) {
        output += scriptText(value, char === "_" ? SUBSCRIPT : SUPERSCRIPT, char);
        i += 2;
        continue;
      }
    }

    output += char;
    i += 1;
  }

  return output.replace(/[ \t]+/g, " ").trim();
}

function balanced(source: string): boolean {
  const pairs: Record<string, string> = { "}": "{", ")": "(", "]": "[" };
  const openings = new Set(Object.values(pairs));
  const stack: string[] = [];
  for (let i = 0; i < source.length; i += 1) {
    if (source[i] === "\\") {
      const command = source.slice(i + 1).match(/^[A-Za-z]+/)?.[0];
      if (command) {
        i += command.length;
        continue;
      }
      i += 1;
      continue;
    }
    if (openings.has(source[i])) stack.push(source[i]);
    else if (pairs[source[i]] && stack.pop() !== pairs[source[i]]) return false;
  }
  return stack.length === 0;
}

export function formatLatexForTerminal(source: string): TerminalMath {
  const trimmed = source.trim();
  return {
    text: convertCore(trimmed) || trimmed,
    malformed: !balanced(trimmed),
  };
}
