import { Children, isValidElement, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import "./styles/feedback.css";

function textOf(node: ReactNode): string {
  return Children.toArray(node).map(child => isValidElement<{children?: ReactNode}>(child) ? textOf(child.props.children) : String(child)).join(" ");
}

/** 操作提示不占页面空间；业务状态仍留在对应内容区。 */
export function Notice({ children, kind = "error", onDismiss }: {children: ReactNode; kind?: string; onDismiss?: () => void}) {
  const message = textOf(children);
  const [visible, setVisible] = useState(true);
  const element = useRef<HTMLDivElement>(null);
  const dismissed = useRef(onDismiss);
  dismissed.current = onDismiss;
  const close = () => {setVisible(false); dismissed.current?.();};
  useEffect(() => {
    if (visible && element.current?.showPopover) {element.current.setAttribute("popover", "manual"); element.current.showPopover();}
  }, [visible]);
  useEffect(() => {
    setVisible(true);
    const close = () => {setVisible(false); dismissed.current?.();};
    const timer = window.setTimeout(close, kind === "error" ? 7000 : 4500);
    window.addEventListener("hashchange", close);
    return () => {window.clearTimeout(timer); window.removeEventListener("hashchange", close);};
  }, [message, kind]);
  return visible ? createPortal(<div ref={element} className={"app-notice is-" + kind} role={kind === "error" ? "alert" : "status"}>
    <span>{children}</span><button type="button" aria-label="关闭通知" onClick={close}>×</button>
  </div>, document.body) : null;
}

let dialogCount = 0;
let previousOverflow = "";

/** 使用浏览器顶层弹窗，保留表单归属和原有按需加载。 */
export function DetailDialog({children, className = "", open, onToggle, hideTrigger = false}: {
  children: ReactNode; className?: string; open?: boolean; hideTrigger?: boolean;
  onToggle?: (event: {currentTarget: {open: boolean}}) => void;
}) {
  const parts = Children.toArray(children);
  const heading = parts.find(child => isValidElement(child) && child.type === "summary");
  const title = isValidElement<{children: ReactNode}>(heading) ? heading.props.children : "查看详情";
  const body = parts.filter(child => child !== heading);
  const [visible, setVisible] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const toggle = useRef(onToggle);
  toggle.current = onToggle;
  const id = useId();
  const change = (next: boolean) => {setVisible(next); toggle.current?.({currentTarget:{open:next}});};
  useEffect(() => {if (open !== undefined) setVisible(open);}, [open]);
  useEffect(() => {
    const close = () => {setVisible(false); toggle.current?.({currentTarget:{open:false}});};
    window.addEventListener("hashchange", close);
    return () => window.removeEventListener("hashchange", close);
  }, []);
  useEffect(() => {
    if (!visible) return;
    const element = dialog.current;
    if (!element) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (typeof element.showModal === "function") element.showModal();
    else element.setAttribute("open", "");
    if (dialogCount++ === 0) {previousOverflow = document.body.style.overflow; document.body.style.overflow = "hidden";}
    return () => {
      if (typeof element.close === "function") element.close();
      if (--dialogCount === 0) document.body.style.overflow = previousOverflow;
      if (trigger.current?.isConnected) trigger.current.focus({preventScroll:true});
      else if (previousFocus?.isConnected) previousFocus.focus({preventScroll:true});
    };
  }, [visible]);
  return <div className={"detail-disclosure " + (hideTrigger ? "is-imperative " : "") + className}>
    {!hideTrigger && <button ref={trigger} type="button" className="detail-trigger" aria-haspopup="dialog" aria-expanded={visible} onClick={() => change(true)}>{title}<span aria-hidden="true">↗</span></button>}
    {visible && <dialog ref={dialog} className="detail-dialog workspace-surface" aria-labelledby={id}
      onCancel={event => {event.preventDefault(); change(false);}}
      onClick={event => {if (event.target === event.currentTarget) change(false);}}>
      <div className="detail-dialog-frame"><header className="detail-dialog-header"><h2 id={id}>{title}</h2><button type="button" aria-label="关闭详情" onClick={() => change(false)}>关闭 ×</button></header>
        <div className="detail-dialog-body">{body}</div></div>
    </dialog>}
  </div>;
}
