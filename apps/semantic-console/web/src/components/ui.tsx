import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { InputHTMLAttributes, ReactNode, SelectHTMLAttributes, TextareaHTMLAttributes } from "react";
import { useTranslation } from "react-i18next";
import { CaretLeft, CaretRight, Check, CircleNotch, Info, WarningCircle, X } from "@phosphor-icons/react";
import type { Icon } from "@phosphor-icons/react";
import { translateLegacy } from "../i18n";

export type IconComponent = Icon;

export const PAGE_SIZE_OPTIONS = [20, 50, 100] as const;

export type PaginationProps = {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: number) => void;
};

/** Keep long Console collections bounded while preserving controlled selection state. */
export function usePagination<T>(items: readonly T[], resetKey = "", initialPageSize = PAGE_SIZE_OPTIONS[0]) {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<number>(initialPageSize);
  const pageCount = Math.max(1, Math.ceil(items.length / pageSize));

  useEffect(() => { setPage(1); }, [resetKey]);
  useEffect(() => { setPage((current) => Math.min(current, pageCount)); }, [pageCount]);

  const pageItems = useMemo(
    () => items.slice((page - 1) * pageSize, page * pageSize),
    [items, page, pageSize],
  );

  return {
    pageItems,
    paginationProps: {
      page,
      pageSize,
      total: items.length,
      onPageChange: setPage,
      onPageSizeChange: (nextPageSize: number) => { setPageSize(nextPageSize); setPage(1); },
    } satisfies PaginationProps,
  };
}

export function Pagination({ page, pageSize, total, onPageChange, onPageSizeChange }: PaginationProps) {
  const { t } = useTranslation();
  if (total <= PAGE_SIZE_OPTIONS[0]) return null;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const start = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const end = Math.min(page * pageSize, total);

  return (
    <nav className="pagination" aria-label={t("pagination.label")}>
      <span className="pagination-range" aria-live="polite">{t("pagination.range", { start, end, total })}</span>
      <label className="pagination-size">
        <span>{t("pagination.rowsPerPage")}</span>
        <select value={pageSize} onChange={(event) => onPageSizeChange(Number(event.target.value))}>
          {PAGE_SIZE_OPTIONS.map((option) => <option key={option} value={option}>{option}</option>)}
        </select>
      </label>
      <div className="pagination-pages">
        <button type="button" className="pagination-button" aria-label={t("pagination.previous")} disabled={page <= 1 || total === 0} onClick={() => onPageChange(Math.max(1, page - 1))}><CaretLeft size={15} /></button>
        <PageJump page={page} pageCount={pageCount} disabled={total === 0} onPageChange={onPageChange} />
        <button type="button" className="pagination-button" aria-label={t("pagination.next")} disabled={page >= pageCount || total === 0} onClick={() => onPageChange(Math.min(pageCount, page + 1))}><CaretRight size={15} /></button>
      </div>
    </nav>
  );
}

function PageJump({ page, pageCount, disabled, onPageChange }: { page: number; pageCount: number; disabled: boolean; onPageChange: (page: number) => void }) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState(String(page));
  useEffect(() => setDraft(String(page)), [page]);
  function commit() {
    const parsed = Number.parseInt(draft, 10);
    const next = Number.isFinite(parsed) ? Math.min(pageCount, Math.max(1, parsed)) : page;
    setDraft(String(next));
    onPageChange(next);
  }
  return <label className="pagination-jump"><span>{t("pagination.page")}</span><input type="number" min={1} max={pageCount} value={draft} disabled={disabled} aria-label={t("pagination.jump")} onChange={(event) => setDraft(event.target.value)} onBlur={commit} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); commit(); } }} /><span>{t("pagination.of", { count: pageCount })}</span></label>;
}

function localizeNode(node: ReactNode): ReactNode {
  if (typeof node === "string") return translateLegacy(node);
  if (Array.isArray(node)) return node.map(localizeNode);
  return node;
}

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
  useTranslation();
  return (
    <button
      className={`button button-${variant} button-${size} ${className}`}
      {...props}
      disabled={loading || props.disabled}
    >
      {loading ? <CircleNotch className="spin" size={16} aria-hidden="true" /> : Icon ? <Icon size={16} weight="bold" aria-hidden="true" /> : null}
      <span>{localizeNode(children)}</span>
    </button>
  );
}

export function Badge({ children, tone = "neutral", dot = false }: { children: ReactNode; tone?: "neutral" | "blue" | "green" | "amber" | "red"; dot?: boolean }) {
  useTranslation();
  return <span className={`badge badge-${tone}`}>{dot ? <span className="badge-dot" aria-hidden="true" /> : null}{localizeNode(children)}</span>;
}

export function Label({ children, htmlFor, required = false }: { children: ReactNode; htmlFor?: string; required?: boolean }) {
  useTranslation();
  return <label className="field-label" htmlFor={htmlFor}>{localizeNode(children)}{required ? <span className="required-mark" aria-hidden="true">*</span> : null}</label>;
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
  useTranslation();
  const Icon = tone === "error" || tone === "warning" ? WarningCircle : tone === "success" ? Check : Info;
  return <div className={`notice notice-${tone}`} role={tone === "error" ? "alert" : "status"}><Icon size={18} weight="fill" aria-hidden="true" /><div className="notice-content">{title ? <strong>{translateLegacy(title)}</strong> : null}{children ? <span>{localizeNode(children)}</span> : null}</div>{onDismiss ? <button className="icon-button notice-dismiss" onClick={onDismiss} aria-label="Dismiss message"><X size={16} /></button> : null}</div>;
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <span className={`skeleton ${className}`} aria-hidden="true" />;
}

export function LoadingRows({ count = 4 }: { count?: number }) {
  return <div className="loading-rows" aria-label="Loading"><Skeleton className="loading-row-short" />{Array.from({ length: count }, (_, index) => <Skeleton key={index} className="loading-row" />)}</div>;
}

export function EmptyState({ icon: Icon = Info, title, body, action }: { icon?: IconComponent; title: string; body: string; action?: ReactNode }) {
  useTranslation();
  return <div className="empty-state"><span className="empty-icon"><Icon size={22} weight="duotone" /></span><h3>{translateLegacy(title)}</h3><p>{translateLegacy(body)}</p>{action ? <div className="empty-action">{action}</div> : null}</div>;
}

export function SectionHeading({ eyebrow, title, description, action }: { eyebrow?: string; title: string; description?: string; action?: ReactNode }) {
  useTranslation();
  return <div className="section-heading">{eyebrow ? <p className="eyebrow">{translateLegacy(eyebrow)}</p> : null}<div className="section-heading-row"><div><h1>{translateLegacy(title)}</h1>{description ? <p>{translateLegacy(description)}</p> : null}</div>{action ? <div className="section-heading-action">{action}</div> : null}</div></div>;
}

export function Modal({ open, title, description, onClose, children, footer }: { open: boolean; title: string; description?: string; onClose: () => void; children: ReactNode; footer?: ReactNode }) {
  const { t } = useTranslation();
  const titleId = useId();
  const modalRef = useRef<HTMLElement>(null);
  const onCloseRef = useRef(onClose);
  useEffect(() => { onCloseRef.current = onClose; }, [onClose]);
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const modal = modalRef.current;
    const focusableSelector = "button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex=\"-1\"])";
    const focusFirst = () => (modal?.querySelector<HTMLElement>("[autofocus]") ?? modal?.querySelector<HTMLElement>(focusableSelector))?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); onCloseRef.current(); return; }
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
  }, [open]);
  if (!open) return null;
  return <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}><section ref={modalRef} className="modal" role="dialog" aria-modal="true" aria-labelledby={titleId}><div className="modal-header"><div><h2 id={titleId}>{translateLegacy(title)}</h2>{description ? <p>{translateLegacy(description)}</p> : null}</div><button className="icon-button" onClick={onClose} aria-label={t("common.closeDialog")}><X size={18} /></button></div><div className="modal-body">{children}</div>{footer ? <div className="modal-footer">{footer}</div> : null}</section></div>;
}

export function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (value: boolean) => void; label: string }) {
  return <button type="button" role="switch" aria-checked={checked} aria-label={label} className={`toggle ${checked ? "toggle-on" : ""}`} onClick={() => onChange(!checked)}><span /></button>;
}
