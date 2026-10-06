'use client';

import { useQuery } from '@tanstack/react-query';
import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { apiRequest } from '@/core/api';

type SetupStatus = { setupRequired: boolean };
type Session = { authenticated: true; csrfToken: string };

/** Routes the initial visit to setup, the application, or sign-in from live status checks. */
export default function HomePage() {
  const router = useRouter();
  const setup = useQuery({ queryKey: ['setup-status'], queryFn: () => apiRequest<SetupStatus>('/api/v1/auth/setup-status') });
  const session = useQuery({
    queryKey: ['session'],
    queryFn: () => apiRequest<Session>('/api/v1/auth/session'),
    enabled: setup.data?.setupRequired === false,
  });

  useEffect(() => {
    if (setup.data?.setupRequired) router.replace('/setup');
    else if (setup.data && session.data?.authenticated) router.replace('/app');
    else if (setup.data && session.isError) router.replace('/login');
  }, [router, setup.data, session.data, session.isError]);

  if (setup.isError) return <main className="page"><section className="auth-panel"><span className="brand">Umwelt-OS</span><h1>Can’t reach the local service</h1><p className="muted">Check that the API is running, then reload this page.</p></section></main>;
  return <main className="page"><div className="skeleton" style={{ width: 'min(100%, 480px)' }} aria-label="Loading" /></main>;
}
