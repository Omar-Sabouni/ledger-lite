import type { ButtonHTMLAttributes, HTMLAttributes, ReactNode } from "react";

export type Tone = "positive" | "warning" | "critical" | "neutral" | "info";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "secondary" | "quiet" | "danger";
  busy?: boolean;
}

export function Button({
  children,
  variant = "secondary",
  busy = false,
  disabled,
  className = "",
  ...props
}: ButtonProps) {
  return (
    <button
      className={`button button--${variant} ${className}`}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      {...props}
    >
      <span>{busy ? "Working / " : ""}{children}</span>
    </button>
  );
}

export function StatusPill({
  children,
  tone = "neutral",
  pulse = false
}: {
  children: ReactNode;
  tone?: Tone;
  pulse?: boolean;
}) {
  return (
    <span className={`status-pill status-pill--${tone}`}>
      <span className={`status-dot ${pulse ? "status-dot--pulse" : ""}`} aria-hidden="true" />
      {children}
    </span>
  );
}

export function Eyebrow({ children }: { children: ReactNode }) {
  return <p className="eyebrow">{children}</p>;
}

export function Card({ className = "", ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={`card ${className}`} {...props} />;
}

export function SectionHeading({
  id,
  eyebrow,
  title,
  description,
  action
}: {
  id?: string;
  eyebrow?: string;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <header className="section-heading">
      <div>
        {eyebrow ? <Eyebrow>{eyebrow}</Eyebrow> : null}
        <h1 id={id}>{title}</h1>
        {description ? <p>{description}</p> : null}
      </div>
      {action ? <div className="section-heading__action">{action}</div> : null}
    </header>
  );
}

export function EmptyState({
  code = "--",
  title,
  description,
  action
}: {
  code?: "--" | "00";
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <span className="empty-state__code" aria-hidden="true">{code}</span>
      <h3>{title}</h3>
      <p>{description}</p>
      {action}
    </div>
  );
}

export function ErrorState({
  title = "We couldn't load this view",
  description,
  requestId,
  onRetry,
  headingLevel = "h3"
}: {
  title?: string;
  description: string;
  requestId?: string | undefined;
  onRetry?: (() => void) | undefined;
  headingLevel?: "h2" | "h3";
}) {
  const Heading = headingLevel;
  return (
    <div className="error-state" role="alert">
      <span className="error-state__code" aria-hidden="true">ERR</span>
      <div>
        <Heading>{title}</Heading>
        <p>{description}</p>
        {requestId ? <p className="request-id">Request {requestId}</p> : null}
      </div>
      {onRetry ? <Button onClick={onRetry}>Try again</Button> : null}
    </div>
  );
}

export function PaginationError({
  description,
  onRetry
}: {
  description: string;
  onRetry: () => void;
}) {
  return (
    <div className="pagination-error" role="alert">
      <span>{description}</span>
      <Button variant="quiet" onClick={onRetry}>Try again</Button>
    </div>
  );
}

export function Skeleton({ lines = 3, compact = false }: { lines?: number; compact?: boolean }) {
  return (
    <div className={`skeleton ${compact ? "skeleton--compact" : ""}`} aria-label="Loading content" role="status">
      {Array.from({ length: lines }, (_, index) => <span key={index} />)}
    </div>
  );
}

export function VisuallyHidden({ children }: { children: ReactNode }) {
  return <span className="sr-only">{children}</span>;
}

export function shortId(value: string | null | undefined) {
  if (!value) return "—";
  return `${value.slice(0, 8)}…${value.slice(-4)}`;
}
