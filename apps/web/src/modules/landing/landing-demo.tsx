'use client';

import dynamic from 'next/dynamic';
import {
  ArrowDownIcon,
  ArrowRightIcon,
  BookOpenIcon,
  BriefcaseBusinessIcon,
  CheckIcon,
  CloudSunIcon,
  DatabaseIcon,
  FileTextIcon,
  GlobeIcon,
  HeartPulseIcon,
  LandmarkIcon,
  NewspaperIcon,
  RocketIcon,
  RssIcon,
  ShieldCheckIcon,
  SparklesIcon,
  UsersIcon,
} from 'lucide-react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import { useLocale, useTranslations } from 'next-intl';
import { useEffect, useState, useSyncExternalStore, type ReactNode } from 'react';
import { Button } from '@/components/ui/button';
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet';
import { UmweltLogo, UmweltMark } from '@/components/brand/umwelt-mark';
import { apiRequest } from '@/core/api';
import { useDisplayPreferences } from '@/core/query-provider';
import type { AppLocaleId } from '@/core/i18n';
import type { LandingSourceId } from './source-canvas';

const SourceCanvas = dynamic(() => import('./source-canvas').then((module) => module.SourceCanvas), { ssr: false });
const SOURCE_IDS: LandingSourceId[] = ['website', 'feed', 'document'];
const SOURCE_ICONS = { website: GlobeIcon, feed: RssIcon, document: FileTextIcon } as const;
// Public, offline-readable overview; keep names aligned with docs/connectors/provider-catalog.md.
const SOURCE_CATALOG = [
  { id: 'web', icon: GlobeIcon, items: ['Website', 'RSS / Atom', 'REST API', 'MCP'] },
  { id: 'personal', icon: UsersIcon, items: ['Gmail', 'Google Calendar', 'Google Drive', 'Notion', 'Slack', 'Telegram'] },
  {
    id: 'news',
    icon: NewspaperIcon,
    items: [
      'VnExpress Business',
      'Tuổi Trẻ',
      'Thanh Niên',
      'Dân Trí',
      'BBC World',
      'Al Jazeera',
      'NPR',
      'Google News RSS',
      'GDELT',
      'Finnhub News',
      'NewsAPI',
    ],
  },
  {
    id: 'research',
    icon: BookOpenIcon,
    items: [
      'GitHub',
      'GitHub Releases',
      'GitHub Trending',
      'YouTube',
      'arXiv',
      'OpenAlex',
      'Hugging Face',
      'Hacker News',
      'Lobsters',
      'Wikipedia',
      'Reddit',
      'Bluesky',
      'Mastodon',
      'X',
    ],
  },
  {
    id: 'finance',
    icon: LandmarkIcon,
    items: [
      'Alpha Vantage',
      'SEC EDGAR',
      'World Bank',
      'Frankfurter',
      'ECB',
      'FRED',
      'OECD',
      'Binance',
      'VNDirect',
      'Alternative.me',
      'CoinPaprika',
      'CoinGecko',
      'Fed Press',
      'Open ER API',
    ],
  },
  {
    id: 'environment',
    icon: CloudSunIcon,
    items: [
      'Open-Meteo',
      'OpenWeatherMap',
      'OpenAQ',
      'USGS',
      'NASA EONET',
      'NASA FIRMS',
      'GDACS',
      'US Travel Advisories',
      'NOAA Tsunami',
      'JMA',
      'NWS Alerts',
      'EMSC',
      'BMKG',
    ],
  },
  { id: 'security', icon: ShieldCheckIcon, items: ['CISA KEV', 'IODA', 'NVD'] },
  { id: 'space', icon: RocketIcon, items: ['Launch Library', 'SatNOGS', 'NOAA Kp', 'ISS Position'] },
  { id: 'health', icon: HeartPulseIcon, items: ['FHIR R4 Observations', 'Home Assistant', 'Disease.sh'] },
  { id: 'jobs', icon: BriefcaseBusinessIcon, items: ['We Work Remotely', 'VietnamWorks', 'ITviec'] },
  { id: 'imports', icon: DatabaseIcon, items: ['Markdown', 'OFX', 'CSV'] },
] as const;
type DemoQuestion = 'latest' | 'summarize' | 'compare';

/** Reads only the current sign-in status; workspace content and readiness never gate the public page. */
export function LandingEntry() {
  const session = useQuery({
    queryKey: ['session'],
    queryFn: () => apiRequest<{ authenticated: true; csrfToken: string }>('/api/v1/auth/session'),
  });
  return <LandingPage authenticated={Boolean(session.data?.authenticated)} />;
}

/** Preview card title bar with the "illustrative" disclaimer. */
function DemoHeader() {
  const t = useTranslations('landing');
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border bg-muted px-5 py-4 sm:px-7">
      <div className="flex items-center gap-3">
        <span aria-hidden="true" className="flex size-9 items-center justify-center rounded-lg border border-border bg-background">
          <UmweltMark size={22} />
        </span>
        <h2 id="landing-preview-title" className="text-sm font-semibold text-foreground">
          {t('previewLabel')}
        </h2>
      </div>
      <span className="inline-flex items-center gap-2 text-[13px] text-muted-foreground">
        <span className="size-2 rounded-full bg-data" />
        {t('illustrative')}
      </span>
    </div>
  );
}

/** Props of {@link DemoSourcesPane}. */
type DemoSourcesPaneProps = {
  activeSources: LandingSourceId[];
  selectedCount: number;
  pulseSource: LandingSourceId | null;
  onToggle: (id: LandingSourceId) => void;
};

/** Left preview pane: the WebGL source illustration (with static fallback) and the source toggle buttons. */
function DemoSourcesPane({ activeSources, selectedCount, pulseSource, onToggle }: DemoSourcesPaneProps) {
  const t = useTranslations('landing');
  const [unavailable, setUnavailable] = useState(false);
  const [canvasReady, setCanvasReady] = useState(false);
  const reducedMotion = useReducedMotion();
  return (
    <div className="min-w-0 bg-muted p-5 sm:p-7 lg:border-r lg:border-border">
      <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-base font-semibold text-foreground">{t('sourcesTitle')}</h3>
        <p aria-live="polite" className="text-[13px] text-muted-foreground">
          {t('sourceCount', { count: selectedCount })}
        </p>
      </div>
      <div className="relative h-64 overflow-hidden rounded-xl border border-border bg-background sm:h-72" aria-label={t('canvasLabel')}>
        <div aria-hidden="true" className="pointer-events-none absolute inset-0 flex items-center justify-center">
          <div className="flex size-44 items-center justify-center rounded-full border border-border sm:size-52">
            <div className="size-32 rounded-full border border-border sm:size-40" />
          </div>
        </div>
        <SourceDiagramFallback visuallyHidden={canvasReady && !unavailable} />
        {!unavailable && (
          <SourceCanvas
            activeSources={activeSources}
            pulseSource={pulseSource}
            reducedMotion={reducedMotion}
            onReady={() => setCanvasReady(true)}
            onUnavailable={() => setUnavailable(true)}
          />
        )}
        <div
          aria-hidden="true"
          className="pointer-events-none absolute right-3 top-3 flex items-center gap-2 rounded-full border border-border bg-background px-3 py-2 text-[13px] font-semibold text-foreground shadow-sm"
        >
          <UmweltMark size={20} />
          Umwelt-OS
        </div>
      </div>
      <div className="mt-4 flex flex-wrap gap-2" role="group" aria-label={t('sourcesTitle')}>
        {SOURCE_IDS.map((id) => {
          const Icon = SOURCE_ICONS[id];
          const selected = activeSources.includes(id);
          return (
            <Button
              key={id}
              type="button"
              variant={selected ? 'default' : 'outline'}
              size="sm"
              aria-pressed={selected}
              onClick={() => onToggle(id)}
              className="min-h-11 min-w-32 flex-1 justify-start text-[13px]"
            >
              <Icon aria-hidden="true" className="size-4" />
              <span>{t(`sourceTypes.${id}`)}</span>
              {selected && <CheckIcon aria-hidden="true" className="ml-auto size-4" />}
            </Button>
          );
        })}
      </div>
    </div>
  );
}

/** Props of {@link DemoAnswerPane}. */
type DemoAnswerPaneProps = {
  question: DemoQuestion;
  onQuestion: (question: DemoQuestion) => void;
  selectedSources: LandingSourceId[];
  onOpenReference: (id: LandingSourceId) => void;
};

/** Right preview pane: question chips plus the deterministic sample answer, evidence list and citation buttons. */
function DemoAnswerPane({ question, onQuestion, selectedSources, onOpenReference }: DemoAnswerPaneProps) {
  const t = useTranslations('landing');
  const canShowAnswer = selectedSources.length > 0 && !(question === 'compare' && selectedSources.length < 2);
  const answer =
    selectedSources.length === 0
      ? t('emptySources')
      : question === 'compare' && selectedSources.length < 2
        ? t('compareNeedsSources')
        : t(`answers.${question}`, { sources: selectedSources.map((id) => t(`sourceNames.${id}`)).join(', ') });
  return (
    <div className="flex min-w-0 flex-col p-5 sm:p-7">
      <div className="mb-4 flex items-center gap-3 text-foreground">
        <span className="flex size-9 items-center justify-center rounded-lg bg-secondary text-data">
          <SparklesIcon aria-hidden="true" className="size-4" />
        </span>
        <h3 className="text-base font-semibold">{t('questionTitle')}</h3>
      </div>
      <div className="flex flex-wrap gap-2" role="group" aria-label={t('questionTitle')}>
        {(['latest', 'summarize', 'compare'] as const).map((id) => (
          <Button
            key={id}
            type="button"
            size="sm"
            variant={question === id ? 'secondary' : 'outline'}
            aria-pressed={question === id}
            onClick={() => onQuestion(id)}
          >
            {t(`questions.${id}`)}
          </Button>
        ))}
      </div>
      <div className="mt-5 flex flex-1 flex-col rounded-xl border border-border bg-muted p-5 sm:p-6">
        <div className="mb-4 flex items-center gap-2 text-[13px] font-semibold text-foreground">
          <span className="flex size-7 items-center justify-center rounded-full bg-background">
            <UmweltMark size={17} />
          </span>
          Umwelt AI
        </div>
        <p aria-live="polite" className="text-sm leading-6 text-foreground">
          {answer}
        </p>
        {canShowAnswer && (
          <ul className="mt-4 grid gap-3 text-sm leading-6 text-muted-foreground" aria-label={t('answerEvidence')}>
            {selectedSources.map((id) => (
              <li key={id} className="flex gap-3">
                <span aria-hidden="true" className="mt-2 size-1.5 shrink-0 rounded-full bg-data" />
                {t(`answerEvidenceBySource.${question}.${id}`)}
              </li>
            ))}
          </ul>
        )}
        {canShowAnswer && (
          <div className="mt-6 border-t border-border pt-4">
            <h4 className="mb-3 text-[13px] font-semibold text-muted-foreground">{t('citationsTitle')}</h4>
            <ul className="flex flex-wrap gap-2">
              {selectedSources.map((id) => (
                <li key={id}>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="gap-1.5 bg-background text-[13px]"
                    aria-label={t('openReference', { source: t(`sourceNames.${id}`) })}
                    onClick={() => onOpenReference(id)}
                  >
                    <FileTextIcon aria-hidden="true" className="size-3.5" />
                    {t(`sourceNames.${id}`)}
                    <ArrowRightIcon aria-hidden="true" className="size-3" />
                  </Button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}

/** Side sheet showing the sample reference text for the opened citation; closed when `reference` is null. */
function DemoReferenceSheet({ reference, onClose }: { reference: LandingSourceId | null; onClose: () => void }) {
  const t = useTranslations('landing');
  const activeReference = reference ? t(`sourceNames.${reference}`) : '';
  return (
    <Sheet
      open={reference !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <SheetContent side="right" className="max-w-xl" closeLabel={t('close')}>
        <SheetHeader>
          <SheetTitle>{reference ? t('referenceTitle', { source: activeReference }) : t('citationsTitle')}</SheetTitle>
          <SheetDescription>{t('citationsTitle')}</SheetDescription>
        </SheetHeader>
        {reference && <div className="p-5 text-sm leading-7 text-foreground">{t(`referenceText.${reference}`)}</div>}
      </SheetContent>
    </Sheet>
  );
}

/** Renders a deterministic public preview; source and question selections affect only local sample content. */
export function LandingDemo() {
  const [activeSources, setActiveSources] = useState<LandingSourceId[]>(SOURCE_IDS);
  const [question, setQuestion] = useState<DemoQuestion>('latest');
  const [pulseSource, setPulseSource] = useState<LandingSourceId | null>(null);
  const [reference, setReference] = useState<LandingSourceId | null>(null);
  const selectedSources = SOURCE_IDS.filter((id) => activeSources.includes(id));

  /** Toggles one source and starts a matching brief visual pulse. */
  function toggleSource(id: LandingSourceId): void {
    setActiveSources((current) => (current.includes(id) ? current.filter((item) => item !== id) : [...current, id]));
    setPulseSource(id);
  }

  return (
    <section
      aria-labelledby="landing-preview-title"
      className="mx-auto w-full max-w-7xl overflow-hidden rounded-2xl border border-border bg-background shadow-lg"
    >
      <DemoHeader />
      <div className="grid lg:grid-cols-[1.05fr_0.95fr]">
        <DemoSourcesPane
          activeSources={activeSources}
          selectedCount={selectedSources.length}
          pulseSource={pulseSource}
          onToggle={toggleSource}
        />
        <DemoAnswerPane question={question} onQuestion={setQuestion} selectedSources={selectedSources} onOpenReference={setReference} />
      </div>
      <DemoReferenceSheet reference={reference} onClose={() => setReference(null)} />
    </section>
  );
}

/** Displays a non-WebGL connection map while keeping every source control usable. */
function SourceDiagramFallback({ visuallyHidden = false }: { visuallyHidden?: boolean }) {
  const t = useTranslations('landing');
  return (
    <div
      role="img"
      aria-label={t('diagramFallback')}
      className={visuallyHidden ? 'sr-only' : 'flex h-full flex-col items-center justify-center gap-4 p-6 text-sm text-muted-foreground'}
    >
      <div className="flex flex-wrap items-center justify-center gap-3">
        <span className="rounded-md border border-border bg-background px-3 py-2">{t('sourceTypes.website')}</span>
        <ArrowRightIcon aria-hidden="true" className="size-4" />
        <span className="rounded-md border border-border bg-background px-3 py-2">{t('sourceTypes.feed')}</span>
        <ArrowRightIcon aria-hidden="true" className="size-4" />
        <span className="rounded-md border border-border bg-background px-3 py-2">{t('sourceTypes.document')}</span>
      </div>
      <ArrowDownIcon aria-hidden="true" className="size-4" />
    </div>
  );
}

/** Reads the browser's reduced-motion setting and treats server rendering as reduced. */
function useReducedMotion(): boolean {
  return useSyncExternalStore(
    (listener) => {
      const query = window.matchMedia('(prefers-reduced-motion: reduce)');
      query.addEventListener('change', listener);
      return () => query.removeEventListener('change', listener);
    },
    () => window.matchMedia('(prefers-reduced-motion: reduce)').matches,
    () => true,
  );
}

/** Props shared by the landing sections: auth state picks the call-to-action target, `language` is carried into links. */
type SectionProps = { authenticated: boolean; language: AppLocaleId };

/** Sticky-free top bar: logo, anchor links, language switch and the sign-in / dashboard button. */
function LandingNav({ authenticated, language, onLanguage }: SectionProps & { onLanguage: (next: AppLocaleId) => void }) {
  const t = useTranslations('landing');
  return (
    <header className="border-b border-border bg-background">
      <a href="#main-content" className="sr-only focus:not-sr-only focus:absolute focus:z-50 focus:m-2 focus:bg-background focus:p-2">
        {t('skipToContent')}
      </a>
      <nav
        aria-label={t('navigation')}
        className="mx-auto flex min-h-16 max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-2 sm:px-6 lg:px-8"
      >
        <Link href="/" aria-label="Umwelt-OS" className="rounded-md">
          <UmweltLogo />
        </Link>
        <div className="flex flex-wrap items-center justify-end gap-x-3 gap-y-2 sm:gap-5">
          <a className="hidden text-sm text-muted-foreground hover:text-foreground sm:inline" href="#sources">
            {t('navSources')}
          </a>
          <a className="hidden text-sm text-muted-foreground hover:text-foreground sm:inline" href="#how-it-works">
            {t('navHow')}
          </a>
          <div className="flex items-center gap-1 rounded-md border border-border p-1" role="group" aria-label={t('language')}>
            {(['en-us', 'vi-vi'] as const).map((id) => (
              <Button
                key={id}
                type="button"
                size="sm"
                variant={language === id ? 'secondary' : 'ghost'}
                aria-pressed={language === id}
                onClick={() => onLanguage(id)}
              >
                {id === 'en-us' ? 'EN' : 'VI'}
              </Button>
            ))}
          </div>
          <Button asChild size="sm">
            <Link href={authenticated ? '/dashboard' : `/login?lang=${language}`}>
              {authenticated ? t('openDashboard') : t('signIn')}
              <ArrowRightIcon aria-hidden="true" className="size-4" />
            </Link>
          </Button>
        </div>
      </nav>
    </header>
  );
}

/** Hero headline, primary call to action and the interactive preview. */
function LandingHero({ authenticated, language }: SectionProps) {
  const t = useTranslations('landing');
  return (
    <section className="px-4 pb-12 pt-12 sm:px-6 sm:pb-16 sm:pt-20 lg:px-8">
      <div className="mx-auto max-w-4xl text-center">
        <h1 className="text-4xl font-semibold leading-[1.08] tracking-tight text-foreground sm:text-6xl">
          {t('heroFirst')}
          <span className="mt-2 block text-data">{t('heroSecond')}</span>
        </h1>
        <p className="mx-auto mt-5 max-w-2xl text-base leading-7 text-muted-foreground sm:text-lg">{t('heroDescription')}</p>
        <div className="mt-7 flex flex-wrap items-center justify-center gap-3">
          <Button asChild size="lg">
            <Link href={authenticated ? '/dashboard' : `/login?lang=${language}`}>
              {authenticated ? t('openDashboard') : t('signIn')}
              <ArrowRightIcon aria-hidden="true" className="size-4" />
            </Link>
          </Button>
          <Button asChild size="lg" variant="outline">
            <a href="#sources">
              {t('explore')}
              <ArrowDownIcon aria-hidden="true" className="size-4" />
            </a>
          </Button>
        </div>
      </div>
      <div className="mx-auto mt-10 max-w-7xl sm:mt-14">
        <LandingDemo />
      </div>
    </section>
  );
}

/** Supported source catalog with the connect / manage call to action. */
function LandingSources({ authenticated, language }: SectionProps) {
  const t = useTranslations('landing');
  return (
    <section id="sources" className="scroll-mt-6 border-y border-border bg-secondary px-4 py-14 sm:px-6 sm:py-20 lg:px-8">
      <div className="mx-auto max-w-7xl">
        <div className="flex flex-wrap items-end justify-between gap-6">
          <div>
            <h2 className="text-3xl font-semibold tracking-tight sm:text-4xl">{t('sourcesHeading')}</h2>
            <p className="mt-4 max-w-2xl text-base leading-7 text-muted-foreground">{t('sourcesBody')}</p>
          </div>
          <Button asChild>
            <Link href={authenticated ? '/settings/sources' : `/login?lang=${language}`}>
              {authenticated ? t('manageSources') : t('signInToConnect')}
              <ArrowRightIcon aria-hidden="true" className="size-4" />
            </Link>
          </Button>
        </div>
        <div className="mt-8 grid gap-3 md:grid-cols-2">
          {SOURCE_CATALOG.map(({ id, icon: Icon, items }) => (
            <article key={id} className="rounded-xl border border-border bg-background p-5 sm:p-6">
              <div className="flex items-center gap-3">
                <span className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-secondary text-data">
                  <Icon aria-hidden="true" className="size-5" />
                </span>
                <h3 className="text-base font-semibold">{t(`sourceGroups.${id}`)}</h3>
              </div>
              <ul className="mt-4 flex flex-wrap gap-2">
                {items.map((item) => (
                  <li key={item} className="rounded-md border border-border bg-muted px-2.5 py-1.5 text-sm text-foreground">
                    {item}
                  </li>
                ))}
              </ul>
            </article>
          ))}
        </div>
        <p className="mt-5 max-w-4xl text-sm leading-6 text-muted-foreground">{t('sourcesAvailability')}</p>
      </div>
    </section>
  );
}

/** Capability features, how-it-works steps and privacy links. */
function LandingHowItWorks({ language }: Pick<SectionProps, 'language'>) {
  const t = useTranslations('landing');
  return (
    <>
      <section id="how-it-works" className="scroll-mt-6 px-4 py-14 sm:px-6 sm:py-20 lg:px-8">
        <div className="mx-auto grid max-w-7xl gap-8 sm:grid-cols-3">
          <Feature title={t('aiHeading')} body={t('aiBody')} icon={<SparklesIcon aria-hidden="true" className="size-5" />} />
          <Feature title={t('evidenceHeading')} body={t('evidenceBody')} icon={<FileTextIcon aria-hidden="true" className="size-5" />} />
          <Feature
            title={t('workspaceHeading')}
            body={t('workspaceBody')}
            icon={<ArrowRightIcon aria-hidden="true" className="size-5" />}
          />
        </div>
      </section>

      <section className="border-y border-border bg-secondary px-4 py-14 sm:px-6 sm:py-20 lg:px-8">
        <div className="mx-auto grid max-w-7xl gap-10 sm:grid-cols-2 sm:items-start">
          <div>
            <h2 className="text-3xl font-semibold tracking-tight sm:text-4xl">{t('howHeading')}</h2>
            <ol className="mt-6 grid gap-4">
              {(['stepOne', 'stepTwo', 'stepThree'] as const).map((key, index) => (
                <li key={key} className="flex items-start gap-3">
                  <span className="flex size-7 shrink-0 items-center justify-center rounded-full border border-border bg-background text-[13px] font-semibold">
                    {index + 1}
                  </span>
                  <span className="pt-0.5 text-sm leading-6">{t(key)}</span>
                </li>
              ))}
            </ol>
          </div>
          <div>
            <h2 className="text-3xl font-semibold tracking-tight sm:text-4xl">{t('privacyHeading')}</h2>
            <p className="mt-4 text-base leading-7 text-muted-foreground">{t('privacyBody')}</p>
            <div className="mt-4 flex flex-wrap gap-3">
              <Button asChild variant="outline">
                <Link href={`/privacy?lang=${language}`}>{t('footerPrivacy')}</Link>
              </Button>
              <Button asChild variant="outline">
                <Link href={`/terms?lang=${language}`}>{t('footerTerms')}</Link>
              </Button>
            </div>
          </div>
        </div>
      </section>
    </>
  );
}

/** Frequently asked questions. */
function LandingFaq() {
  const t = useTranslations('landing');
  return (
    <section className="px-4 py-14 sm:px-6 sm:py-20 lg:px-8">
      <div className="mx-auto max-w-5xl">
        <h2 className="text-3xl font-semibold tracking-tight sm:text-4xl">{t('faqHeading')}</h2>
        <div className="mt-7 divide-y divide-border border-y border-border">
          {(['faqSources', 'faqGoogle', 'faqAi'] as const).map((key) => (
            <article key={key} className="py-5">
              <h3 className="text-base font-semibold">{t(`${key}Question`)}</h3>
              <p className="mt-2 text-sm leading-6 text-muted-foreground">{t(`${key}Answer`)}</p>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}

/** Closing call to action followed by the policy footer. */
function LandingFooter({ authenticated, language }: SectionProps) {
  const t = useTranslations('landing');
  return (
    <>
      <section className="bg-primary px-4 py-14 text-primary-foreground sm:px-6 sm:py-16 lg:px-8">
        <div className="mx-auto flex max-w-7xl flex-col items-start justify-between gap-6 sm:flex-row sm:items-center">
          <div>
            <h2 className="text-3xl font-semibold tracking-tight sm:text-4xl">{t('finalHeading')}</h2>
            <p className="mt-3 max-w-xl text-sm leading-6 opacity-80">{t('finalBody')}</p>
          </div>
          <Button
            asChild
            size="lg"
            variant="outline"
            className="border-primary-foreground bg-transparent text-primary-foreground hover:bg-primary-foreground hover:text-primary"
          >
            <Link href={authenticated ? '/dashboard' : `/login?lang=${language}`}>
              {authenticated ? t('openDashboard') : t('signIn')}
              <ArrowRightIcon aria-hidden="true" className="size-4" />
            </Link>
          </Button>
        </div>
      </section>

      <footer className="border-t border-border px-4 py-6 sm:px-6 lg:px-8">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-4">
          <UmweltLogo />
          <nav aria-label={t('footerPrivacy')} className="flex items-center gap-4 text-sm">
            <Link href={`/privacy?lang=${language}`} className="text-muted-foreground hover:text-foreground">
              {t('footerPrivacy')}
            </Link>
            <Link href={`/terms?lang=${language}`} className="text-muted-foreground hover:text-foreground">
              {t('footerTerms')}
            </Link>
          </nav>
        </div>
      </footer>
    </>
  );
}

type LandingPageProps = { authenticated: boolean };

/** Renders the public product page, bilingual controls and entry points to the existing workspace and policies. */
export function LandingPage({ authenticated }: LandingPageProps) {
  const currentLocale = useLocale();
  const locale = currentLocale === 'vi-VN' || currentLocale === 'vi-vi' ? 'vi-vi' : 'en-us';
  const router = useRouter();
  const display = useDisplayPreferences();
  const search = useSyncExternalStore(
    () => () => undefined,
    () => window.location.search,
    () => '',
  );
  const urlLocale = new URLSearchParams(search).get('lang');
  const initial = urlLocale === 'en-us' || urlLocale === 'vi-vi' ? urlLocale : locale;
  // A language picked by the visitor wins over the URL/locale default; null until they choose (no state-in-effect sync).
  const [chosenLanguage, setLanguage] = useState<AppLocaleId | null>(null);
  const language = chosenLanguage ?? initial;
  const { authGeneration, confirmedPreferences, locale: displayLocale, preview, setPreview, timezone } = display;

  useEffect(() => {
    if (!authenticated && initial !== displayLocale) {
      setPreview({ theme: preview?.theme ?? confirmedPreferences?.theme ?? 'system', locale: initial, timezone }, authGeneration);
    }
  }, [initial, authenticated, authGeneration, confirmedPreferences, displayLocale, preview, setPreview, timezone]);

  /** Applies the public language preview and preserves it in the login URL. */
  function changeLanguage(next: AppLocaleId): void {
    setLanguage(next);
    if (!authenticated)
      display.setPreview(
        { theme: display.preview?.theme ?? display.confirmedPreferences?.theme ?? 'system', locale: next, timezone: display.timezone },
        display.authGeneration,
      );
    router.replace(`/?lang=${next}`, { scroll: false });
  }

  return (
    <main id="main-content" lang={language === 'vi-vi' ? 'vi-VN' : 'en-US'} className="min-h-dvh bg-background text-foreground">
      <LandingNav authenticated={authenticated} language={language} onLanguage={changeLanguage} />
      <LandingHero authenticated={authenticated} language={language} />
      <LandingSources authenticated={authenticated} language={language} />
      <LandingHowItWorks language={language} />
      <LandingFaq />
      <LandingFooter authenticated={authenticated} language={language} />
    </main>
  );
}

/** Displays one short product capability using the landing page's shared layout. */
function Feature({ title, body, icon }: { title: string; body: string; icon: ReactNode }) {
  return (
    <article className="rounded-xl border border-border bg-background p-5 sm:p-6">
      <span aria-hidden="true" className="inline-flex size-10 items-center justify-center rounded-lg bg-secondary text-data">
        {icon}
      </span>
      <h2 className="mt-5 text-xl font-semibold leading-7">{title}</h2>
      <p className="mt-3 text-sm leading-6 text-muted-foreground">{body}</p>
    </article>
  );
}
