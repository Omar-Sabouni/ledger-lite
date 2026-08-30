import type {
  CurrencyCode,
  MoneyString,
  SignedMoneyString,
} from "./types";

export const MINOR_UNITS_PER_MAJOR = 100n;
export const MAX_MONEY_WHOLE_DIGITS = 18;

const API_MONEY_PATTERN = /^-?(?:0|[1-9]\d*)\.\d{2}$/;
const INPUT_MONEY_PATTERN = /^[+-]?(?:0|[1-9]\d*)(?:\.\d{1,2})?$/;

function assertReasonableSize(value: string): void {
  const unsigned = value.startsWith("-") || value.startsWith("+")
    ? value.slice(1)
    : value;
  const whole = unsigned.split(".", 1)[0] ?? "";
  if (whole.length > MAX_MONEY_WHOLE_DIGITS) {
    throw new RangeError(
      `Money cannot have more than ${MAX_MONEY_WHOLE_DIGITS} whole-number digits`,
    );
  }
}

function partsToMinorUnits(value: string): bigint {
  const negative = value.startsWith("-");
  const unsigned = value.startsWith("-") || value.startsWith("+")
    ? value.slice(1)
    : value;
  const [whole = "0", fraction = ""] = unsigned.split(".");
  const paddedFraction = fraction.padEnd(2, "0");
  const result = BigInt(whole) * MINOR_UNITS_PER_MAJOR + BigInt(paddedFraction);
  return negative ? -result : result;
}

/** Parse the API's canonical, exactly-two-decimal money representation. */
export function parseMoney(value: MoneyString | SignedMoneyString): bigint {
  if (!API_MONEY_PATTERN.test(value)) {
    throw new TypeError("Money must be a canonical decimal string with two places");
  }
  assertReasonableSize(value);
  return partsToMinorUnits(value);
}

/** Parse user input without ever routing it through a JavaScript number. */
export function parseMoneyInput(value: string): bigint {
  if (!INPUT_MONEY_PATTERN.test(value)) {
    throw new TypeError("Enter a decimal amount with at most two places");
  }
  assertReasonableSize(value);
  return partsToMinorUnits(value);
}

/** Serialize integer minor units to the canonical API representation. */
export function serializeMoney(minorUnits: bigint): SignedMoneyString {
  const negative = minorUnits < 0n;
  const absolute = negative ? -minorUnits : minorUnits;
  const whole = absolute / MINOR_UNITS_PER_MAJOR;
  const fraction = (absolute % MINOR_UNITS_PER_MAJOR).toString().padStart(2, "0");
  return `${negative ? "-" : ""}${whole}.${fraction}`;
}

function groupWholeDigits(value: string): string {
  return value.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

export interface FormatMoneyOptions {
  showPlus?: boolean;
  absolute?: boolean;
  currencyDisplay?: "code" | "none";
}

/**
 * Format money exactly, including values too large for Number to represent.
 * The UI intentionally uses the unambiguous ISO currency code (for example,
 * `−AED 12,500.00`) instead of a locale-dependent symbol.
 */
export function formatMoney(
  value: MoneyString | SignedMoneyString | bigint,
  currency: CurrencyCode = "AED",
  options: FormatMoneyOptions = {},
): string {
  const minorUnits = typeof value === "bigint" ? value : parseMoney(value);
  const isNegative = minorUnits < 0n;
  const absoluteMinorUnits = isNegative ? -minorUnits : minorUnits;
  const canonical = serializeMoney(absoluteMinorUnits);
  const [whole = "0", fraction = "00"] = canonical.split(".");
  const amount = `${groupWholeDigits(whole)}.${fraction}`;
  const sign = options.absolute
    ? ""
    : isNegative
      ? "−"
      : options.showPlus && minorUnits > 0n
        ? "+"
        : "";
  const prefix = options.currencyDisplay === "none" ? "" : `${currency} `;
  return `${sign}${prefix}${amount}`;
}

export function sumMoney(values: readonly SignedMoneyString[]): SignedMoneyString {
  return serializeMoney(values.reduce((sum, value) => sum + parseMoney(value), 0n));
}

export function compareMoney(
  left: SignedMoneyString,
  right: SignedMoneyString,
): -1 | 0 | 1 {
  const leftMinor = parseMoney(left);
  const rightMinor = parseMoney(right);
  return leftMinor < rightMinor ? -1 : leftMinor > rightMinor ? 1 : 0;
}

export function isZeroMoney(value: SignedMoneyString): boolean {
  return parseMoney(value) === 0n;
}

export function absoluteMoney(value: SignedMoneyString): SignedMoneyString {
  const minorUnits = parseMoney(value);
  return serializeMoney(minorUnits < 0n ? -minorUnits : minorUnits);
}
