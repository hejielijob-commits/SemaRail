import { useEffect, useRef } from "react";
import type { InputHTMLAttributes, ReactNode, SelectHTMLAttributes, TextareaHTMLAttributes } from "react";
import { Check, CircleNotch, Info, WarningCircle, X } from "@phosphor-icons/react";
import type { Icon } from "@phosphor-icons/react";

export type IconComponent = Icon;

export function Button({
  children,
  variant = "secondary",
  size = "md",
  loading = false,
  icon: Icon,
  className = "",
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md";
  loading?: boolean;
  icon?: IconComponent;
}) {
  return (
    <button
      className={`button button-${variant} button-${size} ${className}`}
      {...props}
      disabled={loading || props.disabled}
    >
      {loading ? <CircleNotch className="spin" size={16} aria-hidden="true" /> : Icon ? <Icon size={16} weight="bold" aria-hidden="true" /> : null}
      <span>{children}</span>
    </button>
  );
}

export function Badge({ children, tone = "neutral", dot = false }: { children: ReactNode; tone?: "neutral" | "blue" | "green" | "amber" | "red"; dot?: boolean }) {
  return <span className={`badge badge-${tone}`}>{dot ? <span className="badge-dot" aria-hidden="true" /> : null}{children}</span>;
}

export function Label({ children, htmlFor, required = false }: { children: ReactNode; htmlFor?: string; required?: boolean }) {
  return <label className="field-label" htmlFor={htmlFor}>{children}{required ? <span className="required-mark" aria-hidden="true">*</span> : null}</label>;
}

export function TextInput(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input className="input" {...props} />;
}

export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className="input select" {...props} />;
}

export function TextArea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className="input textarea" {...props} />;
}

export function Field({ label, hint, error, required, children, htmlFor }: { label: ReactNode; hint?: ReactNode; error?: ReactNode; required?: boolean; children: ReactNode; htmlFor?: string }) {
  return <div className="field"><Label htmlFor={htmlFor} required={required}>{label}</Label>{children}{hint && !error ? <p className="field-hint">{hint}</p> : null}{error ? <p className="field-error" role="alert">{error}</p> : null}</div>;
}

export function InlineNotice({ tone = "info", title, children, onDismiss }: { tone?: "info" | "success" | "warning" | "error"; title?: string; children?: ReactNode; onDismiss?: () => void }) {
  const Icon = tone === "error" || tone === "warning" ? WarningCircle : tone === "success" ? Check : Info;
  return <div className={`notice notice-${tone}`} role={tone === "error" ? "alert" : "status"}><Icon size={18} weight="fill" aria-hidden="true" /><div className="notice-content">{title ? <strong>{title}</strong> : null}{children ? <span>{children}</span> : null}</div>{onDismiss ? <button className="icon-button notice-dismiss" onClick={onDismiss} aria-label="Dismiss message"><X size={16} /></button> : null}</div>;
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <span className={`skeleton ${className}`} aria-hidden="true" />;
}

export function LoadingRows({ count = 4 }: { count?: number }) {
  return <div className="loading-rows" aria-label="Loading"><Skeleton className="loading-row-short" />{Array.from({ length: count }, (_, index) => <Skeleton key={index} className="loading-row" />)}</div>;
}

export function EmptyState({ icon: Icon = Info, title, body, action }: { icon?: IconComponent; title: string; body: string; action?: ReactNode }) {
  return <div className="empty-state"><span className="empty-icon"><Icon size={22} weight="duotone" /></span><h3>{title}</h3><p>{body}</p>{action ? <div className="empty-action">{action}</div> : null}</div>;
}

export function SectionHeading({ eyebrow, title, description, action }: { eyebrow?: string; title: string; description?: string; action?: ReactNode }) {
  return <div className="section-heading">{eyebrow ? <p className="eyebrow">{eyebrow}</p> : null}<div className="section-heading-row"><div><h1>{title}</h1>{description ? <p>{description}</p> : null}</div>{action ? <div className="section-heading-action">{action}</div> : null}</div></div>;
}

export function Modal({ open, title, description, onClose, children, footer }: { open: boolean; title: string; description?: string; onClose: () => void; children: ReactNode; footer?: ReactNode }) {
  const modalRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const modal = modalRef.current;
    const focusableSelector = "button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex=\"-1\"])";
    const focusFirst = () => modal?.querySelector<HTMLElement>(focusableSelector)?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); onClose(); return; }
      if (event.key !== "Tab" || !modal) return;
      const focusable = Array.from(modal.querySelectorAll<HTMLElement>(focusableSelector));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    const timer = window.setTimeout(focusFirst, 0);
    document.addEventListener("keydown", onKeyDown);
    return () => { window.clearTimeout(timer); document.removeEventListener("keydown", onKeyDown); previous?.focus(); };
  }, [open, onClose]);
  if (!open) return null;
  return <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}><section ref={modalRef} className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title"><div className="modal-header"><div><h2 id="modal-title">{title}</h2>{description ? <p>{description}</p> : null}</div><button className="icon-button" onClick={onClose} aria-label="Close dialog"><X size={18} /></button></div><div className="modal-body">{children}</div>{footer ? <div className="modal-footer">{footer}</div> : null}</section></div>;
}

export function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (value: boolean) => void; label: string }) {
  return <button type="button" role="switch" aria-checked={checked} aria-label={label} className={`toggle ${checked ? "toggle-on" : ""}`} onClick={() => onChange(!checked)}><span /></button>;
}
