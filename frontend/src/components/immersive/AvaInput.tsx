import { useEffect, useRef, useState } from 'react';
import { useImmersiveStore } from './immersiveStore';
import { useViewportScale } from './useViewportScale';

interface AvaInputProps {
  onAsk: (text: string) => void;
  disabled?: boolean;
}

export function AvaInput({ onAsk, disabled }: AvaInputProps) {
  const [value, setValue] = useState('');
  const state = useImmersiveStore((s) => s.state);
  const vp = useViewportScale();
  const inputRef = useRef<HTMLInputElement | null>(null);

  const isThinkingOrSpeaking = state === 'thinking' || state === 'speaking';
  const isBusy = !!disabled || isThinkingOrSpeaking;

  // Focus on mount + after each turn completes
  useEffect(() => {
    if (!isBusy) {
      const t = setTimeout(() => inputRef.current?.focus(), 50);
      return () => clearTimeout(t);
    }
  }, [isBusy]);

  function submit() {
    const t = value.trim();
    if (!t || isBusy) return;
    onAsk(t);
    setValue('');
  }

  const fontSize = vp.isMobile ? 12 : 14;

  return (
    <div
      style={{
        position: 'fixed',
        bottom: vp.isMobile ? 92 : 150,
        left: '50%',
        transform: 'translateX(-50%)',
        width: 'min(720px, 86vw)',
        zIndex: 150,
        pointerEvents: 'auto',
      }}
    >
      <form onSubmit={(e) => { e.preventDefault(); submit(); }}>
        <div
          style={{
            position: 'relative',
            borderBottom: `1px solid ${isBusy ? 'rgba(81,164,222,0.2)' : 'rgba(81,164,222,0.4)'}`,
            transition: 'border-color 0.3s',
          }}
        >
          {/* Leading glyph */}
          <span
            style={{
              position: 'absolute',
              left: 0,
              top: '50%',
              transform: 'translateY(-50%)',
              color: '#2a5a7a',
              fontFamily: "'JetBrains Mono', monospace",
              fontSize: fontSize - 1,
              letterSpacing: '0.2em',
              pointerEvents: 'none',
            }}
          >
            &gt;
          </span>
          <input
            ref={inputRef}
            type="text"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={isBusy ? '...' : 'parle à Ava'}
            disabled={isBusy}
            spellCheck={false}
            autoComplete="off"
            style={{
              width: '100%',
              background: 'transparent',
              border: 'none',
              color: '#e5e5ea',
              fontFamily: "'JetBrains Mono', monospace",
              fontSize,
              letterSpacing: '0.06em',
              padding: '10px 0 10px 22px',
              outline: 'none',
              caretColor: '#51a4de',
            }}
          />
          {/* Trailing hint (Enter) */}
          {!isBusy && value.length > 0 && (
            <span
              style={{
                position: 'absolute',
                right: 0,
                top: '50%',
                transform: 'translateY(-50%)',
                color: '#51a4de',
                fontFamily: "'JetBrains Mono', monospace",
                fontSize: fontSize - 3,
                letterSpacing: '0.25em',
                opacity: 0.7,
              }}
            >
              ⏎
            </span>
          )}
        </div>
      </form>
    </div>
  );
}
