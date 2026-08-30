import type { IsoTimestamp } from "./types";

export const DUBAI_TIME_ZONE = "Asia/Dubai";
export const UTC_TIME_ZONE = "UTC";

const DUBAI_DATE_TIME_FORMATTER = new Intl.DateTimeFormat("en-AE", {
  timeZone: DUBAI_TIME_ZONE,
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

const DUBAI_DATE_FORMATTER = new Intl.DateTimeFormat("en-AE", {
  timeZone: DUBAI_TIME_ZONE,
  day: "2-digit",
  month: "short",
  year: "numeric",
});

const UTC_DATE_TIME_FORMATTER = new Intl.DateTimeFormat("en-GB", {
  timeZone: UTC_TIME_ZONE,
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

const RELATIVE_FORMATTER = new Intl.RelativeTimeFormat("en", {
  numeric: "auto",
  style: "long",
});

export function parseTimestamp(value: IsoTimestamp | Date): Date {
  if (value instanceof Date) {
    if (Number.isNaN(value.getTime())) {
      throw new RangeError("Timestamp is not a valid date");
    }
    return new Date(value.getTime());
  }

  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    throw new RangeError("Timestamp is not a valid ISO date-time");
  }
  return parsed;
}

export function formatDubaiDateTime(value: IsoTimestamp | Date): string {
  return `${DUBAI_DATE_TIME_FORMATTER.format(parseTimestamp(value))} GST`;
}

export function formatDubaiDate(value: IsoTimestamp | Date): string {
  return DUBAI_DATE_FORMATTER.format(parseTimestamp(value));
}

export function formatUtcDateTime(value: IsoTimestamp | Date): string {
  return `${UTC_DATE_TIME_FORMATTER.format(parseTimestamp(value))} UTC`;
}

export interface DualTimestamp {
  dubai: string;
  utc: string;
  iso: IsoTimestamp;
}

export function formatDualTimestamp(value: IsoTimestamp | Date): DualTimestamp {
  const date = parseTimestamp(value);
  return {
    dubai: formatDubaiDateTime(date),
    utc: formatUtcDateTime(date),
    iso: date.toISOString(),
  };
}

export function formatRelativeTime(
  value: IsoTimestamp | Date,
  now: Date = new Date(),
): string {
  const differenceSeconds = Math.round(
    (parseTimestamp(value).getTime() - parseTimestamp(now).getTime()) / 1_000,
  );
  const absoluteSeconds = Math.abs(differenceSeconds);

  if (absoluteSeconds < 60) {
    return RELATIVE_FORMATTER.format(differenceSeconds, "second");
  }
  const differenceMinutes = Math.round(differenceSeconds / 60);
  if (Math.abs(differenceMinutes) < 60) {
    return RELATIVE_FORMATTER.format(differenceMinutes, "minute");
  }
  const differenceHours = Math.round(differenceMinutes / 60);
  if (Math.abs(differenceHours) < 24) {
    return RELATIVE_FORMATTER.format(differenceHours, "hour");
  }
  const differenceDays = Math.round(differenceHours / 24);
  if (Math.abs(differenceDays) < 30) {
    return RELATIVE_FORMATTER.format(differenceDays, "day");
  }
  const differenceMonths = Math.round(differenceDays / 30);
  if (Math.abs(differenceMonths) < 12) {
    return RELATIVE_FORMATTER.format(differenceMonths, "month");
  }
  return RELATIVE_FORMATTER.format(Math.round(differenceMonths / 12), "year");
}
