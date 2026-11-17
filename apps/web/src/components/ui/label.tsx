import type { LabelHTMLAttributes } from 'react';

/** Renders the label UI wrapper and forwards its typed props to the underlying control. */
export function Label(props: LabelHTMLAttributes<HTMLLabelElement>) {
  return <label className="label" {...props} />;
}
