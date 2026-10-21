import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { PolicyDocument } from './policy-document';

vi.mock('next-intl', () => ({ useLocale: () => 'en-US' }));

describe('public policy page', () => {
  it('uses the explicit Vietnamese language, labels and public navigation', () => {
    render(<PolicyDocument name="privacy" language="vi-vi" documents={{ 'en-us': '<h1>Privacy</h1>', 'vi-vi': '<h1>Quyền riêng tư</h1>' }} inventory="<p>Inventory</p>" />);
    expect(screen.getByRole('main').getAttribute('lang')).toBe('vi-VN');
    expect(screen.getByRole('heading', { name: 'Quyền riêng tư' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Điều khoản' }).getAttribute('href')).toBe('/terms?lang=vi-vi');
    expect(screen.getByRole('link', { name: 'Đăng nhập' }).getAttribute('href')).toBe('/login');
  });
});
