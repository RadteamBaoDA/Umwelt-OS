'use client';

import { EyeIcon, EyeOffIcon } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { useState, type InputHTMLAttributes } from 'react';
import { Input } from './input';

/**
 * Password field with an eye toggle that reveals the value as plain text.
 * Visibility is local, per-field UI state only; the value is never stored or sent anywhere extra.
 * `type` is owned by the component and ignored if passed.
 */
export function PasswordInput({ style, ...props }: Omit<InputHTMLAttributes<HTMLInputElement>, 'type'>) {
  const t = useTranslations('common');
  const [visible, setVisible] = useState(false);
  const label = visible ? t('hidePassword') : t('showPassword');
  return (
    <span style={{ position: 'relative', display: 'block' }}>
      <Input {...props} type={visible ? 'text' : 'password'} style={{ paddingRight: '2.5rem', ...style }} />
      <button
        type="button"
        aria-label={label}
        aria-pressed={visible}
        title={label}
        onClick={() => setVisible((v) => !v)}
        style={{ position: 'absolute', top: 0, bottom: 0, right: 0, width: '2.5rem', display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'none', border: 0, cursor: 'pointer', color: 'inherit', opacity: 0.7 }}
      >
        {visible ? <EyeOffIcon size={16} aria-hidden /> : <EyeIcon size={16} aria-hidden />}
      </button>
    </span>
  );
}
