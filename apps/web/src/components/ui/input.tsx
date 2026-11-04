import type { InputHTMLAttributes } from 'react';
import { cn } from '@/lib/utils';

/** Renders the input UI wrapper and forwards its typed props to the underlying control. */
export function Input({ className = '', ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cn("input", className)} {...props} />;
}
