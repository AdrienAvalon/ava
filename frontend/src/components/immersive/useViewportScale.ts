import { useEffect, useState } from 'react';

export interface ViewportScale {
  width: number;
  height: number;
  isMobile: boolean;
  isTablet: boolean;
  isNarrow: boolean;
  fontScale: number;    // 1.0 desktop → 0.7 mobile
  orbScale: number;     // overall orb group scale factor
  hideCognitive: boolean;
  hideKatakana: boolean;
  smallHud: boolean;
}

function compute(w: number, h: number): ViewportScale {
  const isMobile = w < 640;
  const isTablet = w < 960;
  const isNarrow = w < 1200;
  return {
    width: w,
    height: h,
    isMobile,
    isTablet,
    isNarrow,
    fontScale: isMobile ? 0.58 : isTablet ? 0.75 : 1.0,
    orbScale: isMobile ? 0.85 : isTablet ? 1.0 : 1.3,
    hideCognitive: isMobile,       // cognitive panel hidden on phones
    hideKatakana: isTablet,        // right-edge katakana hidden on tablets and smaller
    smallHud: isTablet,
  };
}

export function useViewportScale(): ViewportScale {
  const [scale, setScale] = useState<ViewportScale>(() =>
    compute(
      typeof window !== 'undefined' ? window.innerWidth : 1600,
      typeof window !== 'undefined' ? window.innerHeight : 900,
    ),
  );
  useEffect(() => {
    let raf = 0;
    const onResize = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        setScale(compute(window.innerWidth, window.innerHeight));
      });
    };
    window.addEventListener('resize', onResize);
    return () => {
      window.removeEventListener('resize', onResize);
      cancelAnimationFrame(raf);
    };
  }, []);
  return scale;
}
