import {
  Cloud,
  CloudDrizzle,
  CloudFog,
  CloudLightning,
  CloudMoon,
  CloudRain,
  CloudSnow,
  CloudSun,
  Moon,
  Sun,
  type LucideIcon,
} from 'lucide-react';
import { useCallback, useSyncExternalStore } from 'react';

// Recharts-free weather helpers, split from weather-card.tsx so the header summary does not pull the chart library.

export type ConditionKey =
  | 'weatherClear'
  | 'weatherMainlyClear'
  | 'weatherPartlyCloudy'
  | 'weatherCloudy'
  | 'weatherFog'
  | 'weatherDrizzle'
  | 'weatherRain'
  | 'weatherSnow'
  | 'weatherShowers'
  | 'weatherThunder';
export type Unit = 'c' | 'f';
/** Per-device temperature unit choice; Celsius unless the owner picked Fahrenheit. */
export const WEATHER_UNIT_KEY = 'umwelt.weather.unit';
/** Ordered WMO code rules for the non-clear conditions; the first match wins (unknown codes read as cloudy). */
const CONDITION_RULES: { match: (code: number) => boolean; key: ConditionKey; Icon: LucideIcon }[] = [
  { match: (code) => code === 45 || code === 48, key: 'weatherFog', Icon: CloudFog },
  { match: (code) => code >= 51 && code <= 57, key: 'weatherDrizzle', Icon: CloudDrizzle },
  { match: (code) => code >= 61 && code <= 67, key: 'weatherRain', Icon: CloudRain },
  { match: (code) => (code >= 71 && code <= 77) || code === 85 || code === 86, key: 'weatherSnow', Icon: CloudSnow },
  { match: (code) => code >= 80 && code <= 82, key: 'weatherShowers', Icon: CloudRain },
  { match: (code) => code >= 95, key: 'weatherThunder', Icon: CloudLightning },
];

/**
 * Maps a WMO weather code (Open-Meteo `weather_code`) to a condition message key and icon; night swaps sun for moon.
 * Unknown codes read as cloudy rather than failing.
 * @param code WMO code from the provider.
 * @param isDay Provider day flag; only an explicit false selects night icons.
 */
export function weatherCondition(code: number, isDay: boolean | null | undefined): { key: ConditionKey; Icon: LucideIcon } {
  const night = isDay === false;
  if (code === 0) return { key: 'weatherClear', Icon: night ? Moon : Sun };
  if (code === 1 || code === 2)
    return { key: code === 1 ? 'weatherMainlyClear' : 'weatherPartlyCloudy', Icon: night ? CloudMoon : CloudSun };
  const rule = CONDITION_RULES.find((candidate) => candidate.match(code));
  return rule ? { key: rule.key, Icon: rule.Icon } : { key: 'weatherCloudy', Icon: Cloud };
}

/** Converts Celsius to the chosen unit. */
export const toUnit = (celsius: number, unit: Unit) => (unit === 'f' ? (celsius * 9) / 5 + 32 : celsius);

const UNIT_EVENT = 'umwelt:weather-unit';

/**
 * Per-device °C/°F choice shared by the header summary and the card (same-tab event + cross-tab storage event).
 * Server render and blocked storage read Celsius; a failed write keeps the old value.
 * @returns The current unit and a setter that persists it.
 */
export function useWeatherUnit(): [Unit, (next: Unit) => void] {
  const unit = useSyncExternalStore(
    (notify) => {
      window.addEventListener(UNIT_EVENT, notify);
      window.addEventListener('storage', notify);
      return () => {
        window.removeEventListener(UNIT_EVENT, notify);
        window.removeEventListener('storage', notify);
      };
    },
    (): Unit => {
      try {
        return localStorage.getItem(WEATHER_UNIT_KEY) === 'f' ? 'f' : 'c';
      } catch {
        return 'c';
      }
    },
    (): Unit => 'c',
  );
  const setUnit = useCallback((next: Unit) => {
    try {
      localStorage.setItem(WEATHER_UNIT_KEY, next);
    } catch {
      /* storage blocked */
    }
    window.dispatchEvent(new Event(UNIT_EVENT));
  }, []);
  return [unit, setUnit];
}
