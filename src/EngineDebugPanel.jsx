import React, { useState, useCallback } from 'react';
import axios from 'axios';

const API_URL = 'https://aura-trade-v1.onrender.com';

// ─── tiny helpers ────────────────────────────────────────────────────────────
const ts2date = (ts) => {
  if (!ts) return '—';
  let s = Number(ts);
  if (s > 2_000_000_000) s = Math.floor(s / 1000);
  return new Date(s * 1000).toLocaleString('en-GB', {
    month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit',
  });
};

const Badge = ({ children, color = '#2962FF', bg }) => (
  <span style={{
    display: 'inline-block', padding: '2px 8px', borderRadius: 4,
    background: bg || `${color}22`, color, border: `1px solid ${color}55`,
    fontSize: 11, fontWeight: 700, fontFamily: 'monospace',
  }}>{children}</span>
);

const Row = ({ label, value, accent, mono }) => (
  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center',
    padding: '5px 0', borderBottom: '1px solid #2B2B43' }}>
    <span style={{ fontSize: 12, color: '#9B9EA8' }}>{label}</span>
    <span style={{ fontSize: 12, fontWeight: 600, fontFamily: mono ? 'monospace' : 'inherit',
      color: accent || '#E0E0E0' }}>{value ?? '—'}</span>
  </div>
);

const Section = ({ title, color = '#4B8EFF', children }) => (
  <div style={{ marginBottom: 16 }}>
    <div style={{ fontSize: 11, fontWeight: 800, color, letterSpacing: 1,
      textTransform: 'uppercase', marginBottom: 6, paddingBottom: 4,
      borderBottom: `2px solid ${color}44` }}>{title}</div>
    {children}
  </div>
);

// ─── swing candidate table ───────────────────────────────────────────────────
const SwingTable = ({ rows, scoreKey, label, chosenIdx }) => {
  if (!rows || rows.length === 0) return <div style={{ color: '#666', fontSize: 11 }}>None found</div>;
  const maxScore = Math.max(...rows.map(r => r[scoreKey] || 0));
  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11, fontFamily: 'monospace' }}>
        <thead>
          <tr style={{ background: '#1A1E2E' }}>
            {['#', 'Price', 'Date', scoreKey === 'drop_after' ? 'Drop After' : 'Rally After', 'Bar %', 'Chosen'].map(h => (
              <th key={h} style={{ padding: '4px 8px', textAlign: 'left', color: '#666', fontWeight: 700 }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const score    = r[scoreKey] || 0;
            const barPct   = maxScore > 0 ? (score / maxScore) * 100 : 0;
            const isChosen = r.is_chosen || r.candle_idx === chosenIdx;
            return (
              <tr key={i} style={{ background: isChosen ? '#1B2D1B' : i % 2 === 0 ? '#161A25' : '#1A1E2A',
                border: isChosen ? '1px solid #26A69A' : 'none' }}>
                <td style={{ padding: '4px 8px', color: '#666' }}>{i + 1}</td>
                <td style={{ padding: '4px 8px', color: isChosen ? '#26A69A' : '#E0E0E0', fontWeight: isChosen ? 700 : 400 }}>
                  {r.price}
                </td>
                <td style={{ padding: '4px 8px', color: '#9B9EA8' }}>{ts2date(r.date) || r.date}</td>
                <td style={{ padding: '4px 8px', color: score === maxScore ? '#FFD700' : '#E0E0E0',
                  fontWeight: score === maxScore ? 700 : 400 }}>
                  {score.toFixed(2)}
                </td>
                <td style={{ padding: '4px 8px', minWidth: 80 }}>
                  <div style={{ background: '#2B2B43', borderRadius: 3, height: 8, width: '100%', overflow: 'hidden' }}>
                    <div style={{ width: `${barPct}%`, height: '100%',
                      background: isChosen ? '#26A69A' : score === maxScore ? '#FFD700' : '#4B8EFF',
                      transition: 'width 0.3s' }} />
                  </div>
                </td>
                <td style={{ padding: '4px 8px' }}>
                  {isChosen ? <Badge color="#26A69A">✓ CHOSEN</Badge> : ''}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
};

// ─── main component ──────────────────────────────────────────────────────────
const EngineDebugPanel = ({ currency = 'XAUUSD', currentPrice = 0, candles = null }) => {
  const [data,    setData]    = useState(null);
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState(null);
  const [tab,     setTab]     = useState('anchor'); // anchor | bos | ob | raw

  const runDebug = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const body = { currency, current_price: currentPrice, timeframe: '1h' };
      if (candles && candles.length > 50) body.candles = candles;
      const res = await axios.post(`${API_URL}/api/debug`, body);
      setData(res.data);
    } catch (e) {
      setError(e.response?.data?.detail || e.message || 'Request failed');
    } finally {
      setLoading(false);
    }
  }, [currency, currentPrice, candles]);

  // ── styles ────────────────────────────────────────────────────────────────
  const panel = {
    background: '#0F1117', border: '1px solid #2B2B43', borderRadius: 8,
    padding: 16, fontFamily: 'sans-serif', color: '#E0E0E0',
  };
  const tabBtn = (id) => ({
    padding: '6px 14px', border: 'none', cursor: 'pointer', borderRadius: '4px 4px 0 0',
    fontWeight: 700, fontSize: 11, letterSpacing: 0.5,
    background: tab === id ? '#1E2235' : 'transparent',
    color:      tab === id ? '#4B8EFF' : '#666',
    borderBottom: tab === id ? '2px solid #4B8EFF' : '2px solid transparent',
    transition: 'all 0.15s',
  });

  // ── derived values ────────────────────────────────────────────────────────
  const anchorIdx = data?.['🎯 ANCHOR CANDLE'];
  const cycle     = data?.['🎯 CYCLE'];
  const isValid   = data && !data.error;

  return (
    <div style={panel}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <div>
          <div style={{ fontSize: 14, fontWeight: 800, color: '#FFF', letterSpacing: 0.5 }}>
            🔬 ENGINE VERIFICATION
          </div>
          <div style={{ fontSize: 11, color: '#666', marginTop: 2 }}>
            Cross-check every engine decision against your chart
          </div>
        </div>
        <button onClick={runDebug} disabled={loading} style={{
          background: loading ? '#2B2B43' : '#2962FF', color: '#FFF',
          border: 'none', borderRadius: 6, padding: '8px 16px',
          fontWeight: 700, fontSize: 12, cursor: loading ? 'wait' : 'pointer',
          transition: 'background 0.2s',
        }}>
          {loading ? '⏳ Analyzing...' : '▶ RUN DEBUG'}
        </button>
      </div>

      {error && (
        <div style={{ background: '#2D1B1B', border: '1px solid #F23645', borderRadius: 6,
          padding: '8px 12px', fontSize: 12, color: '#F23645', marginBottom: 12 }}>
          ❌ {error}
        </div>
      )}

      {!isValid && !loading && !error && (
        <div style={{ textAlign: 'center', padding: '24px 0', color: '#444', fontSize: 13 }}>
          Click <strong>RUN DEBUG</strong> to verify what the engine computed
        </div>
      )}

      {isValid && (
        <>
          {/* Quick summary bar */}
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 14,
            padding: '10px 12px', background: '#161A25', borderRadius: 6,
            border: `2px solid ${cycle === 'BEARISH' ? '#F23645' : '#26A69A'}44` }}>
            <Badge color={cycle === 'BEARISH' ? '#F23645' : '#26A69A'}>
              {cycle === 'BEARISH' ? '🐻 BEARISH' : '🐂 BULLISH'}
            </Badge>
            <Badge color="#FFD700">⚓ {data['🎯 ANCHOR PRICE']}</Badge>
            <Badge color="#4B8EFF">📊 {data['📊 PHASE']}</Badge>
            <Badge color="#9B9EA8">ATR {data['📏 ATR (14)']}</Badge>
            <Badge color="#9B9EA8">Swing Order {data['🔍 SWING ORDER']}</Badge>
            <Badge color="#9B9EA8">{data['📈 TOTAL CANDLES']} candles</Badge>
          </div>

          {/* Tab bar */}
          <div style={{ display: 'flex', gap: 0, marginBottom: 0, borderBottom: '1px solid #2B2B43' }}>
            {[
              { id: 'anchor', label: '⚓ Anchor Verify' },
              { id: 'bos',    label: '🔵 BOS Lines' },
              { id: 'ob',     label: '🟥 Order Blocks' },
              { id: 'raw',    label: '🔩 Raw Data' },
            ].map(t => (
              <button key={t.id} style={tabBtn(t.id)} onClick={() => setTab(t.id)}>
                {t.label}
              </button>
            ))}
          </div>

          <div style={{ paddingTop: 14 }}>

            {/* ── ANCHOR TAB ─────────────────────────────────────────────── */}
            {tab === 'anchor' && (
              <div>
                <div style={{ background: '#1B2A1B', border: '1px solid #26A69A44', borderRadius: 6,
                  padding: '10px 14px', marginBottom: 14, fontSize: 12 }}>
                  <div style={{ fontWeight: 700, color: '#26A69A', marginBottom: 6 }}>
                    ✅ HOW TO VERIFY ON CHART
                  </div>
                  <div style={{ color: '#9B9EA8', lineHeight: 1.6 }}>
                    1. Find the <span style={{ color: '#FFD700', fontWeight: 700 }}>gold horizontal line</span> on your chart — that's the anchor.<br/>
                    2. Compare its price <Badge color="#FFD700">{data['🎯 ANCHOR PRICE']}</Badge> to the actual candle high at that level.<br/>
                    3. The table below ranks ALL swing candidates — the chosen one should have the highest "drop/rally after" score.<br/>
                    4. If a different peak looks more obvious to you, check its score — it should be close to the winner.
                  </div>
                </div>

                <Section title="Anchor Decision" color="#FFD700">
                  <Row label="Chosen Cycle"    value={cycle}                         accent={cycle === 'BEARISH' ? '#F23645' : '#26A69A'} />
                  <Row label="Anchor Price"    value={data['🎯 ANCHOR PRICE']}        accent="#FFD700" mono />
                  <Row label="High Score"      value={`${data['anchor_high_score']} pts drop`} mono />
                  <Row label="Low Score"       value={`${data['anchor_low_score']} pts rally`} mono />
                  <Row label="Winner"          value={
                    data['anchor_high_score'] > data['anchor_low_score']
                      ? `HIGH wins by ${(data['anchor_high_score'] - data['anchor_low_score']).toFixed(2)} pts`
                      : `LOW wins by ${(data['anchor_low_score'] - data['anchor_high_score']).toFixed(2)} pts`
                  } accent="#FFD700" />
                </Section>

                <Section title={`All Swing High Candidates (ranked by drop)`} color="#F23645">
                  <div style={{ fontSize: 11, color: '#666', marginBottom: 6 }}>
                    ✓ = engine chose this one. Gold bar = highest scorer.
                  </div>
                  <SwingTable
                    rows={data['swing_highs_scored']}
                    scoreKey="drop_after"
                    chosenIdx={data['anchor_high_idx']}
                  />
                </Section>

                <Section title={`All Swing Low Candidates (ranked by rally)`} color="#26A69A">
                  <SwingTable
                    rows={data['swing_lows_scored']}
                    scoreKey="rally_after"
                    chosenIdx={data['anchor_low_idx']}
                  />
                </Section>
              </div>
            )}

            {/* ── BOS TAB ────────────────────────────────────────────────── */}
            {tab === 'bos' && (
              <div>
                <div style={{ background: '#1A1E2E', border: '1px solid #4B8EFF44',
                  borderRadius: 6, padding: '10px 14px', marginBottom: 14, fontSize: 12 }}>
                  <div style={{ fontWeight: 700, color: '#4B8EFF', marginBottom: 6 }}>
                    ✅ HOW TO VERIFY BOS ON CHART
                  </div>
                  <div style={{ color: '#9B9EA8', lineHeight: 1.6 }}>
                    Each BOS line should connect two swing extremes. The level shown is the
                    <strong style={{ color: '#FFF' }}> prior swing that was broken</strong>.<br/>
                    Rule: BOS 1 should be the first time price makes a <em>lower low</em> (bearish) or
                    <em> higher high</em> (bullish) AFTER a real pullback from the anchor.
                  </div>
                </div>

                {data['🔵 BOS LINES']?.length === 0 ? (
                  <div style={{ color: '#666', fontSize: 12, padding: '12px 0' }}>
                    No BOS lines drawn yet — market still in initial impulse phase.
                    This is correct if price has only moved in one direction from the anchor.
                  </div>
                ) : (
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11, fontFamily: 'monospace' }}>
                    <thead>
                      <tr style={{ background: '#1A1E2A' }}>
                        {['Label', 'Level Broken', 'From', 'To (BOS candle)'].map(h => (
                          <th key={h} style={{ padding: '6px 10px', textAlign: 'left', color: '#666', fontWeight: 700 }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {data['🔵 BOS LINES'].map((b, i) => (
                        <tr key={i} style={{ background: i % 2 === 0 ? '#161A25' : '#1A1E2A' }}>
                          <td style={{ padding: '6px 10px' }}><Badge color="#4B8EFF">{b.label}</Badge></td>
                          <td style={{ padding: '6px 10px', color: '#FFD700', fontWeight: 700 }}>{b.level}</td>
                          <td style={{ padding: '6px 10px', color: '#9B9EA8' }}>{ts2date(b.start_time)}</td>
                          <td style={{ padding: '6px 10px', color: '#9B9EA8' }}>{ts2date(b.end_time)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            )}

            {/* ── OB TAB ─────────────────────────────────────────────────── */}
            {tab === 'ob' && (
              <div>
                <div style={{ background: '#2D1B1B', border: '1px solid #F2364544',
                  borderRadius: 6, padding: '10px 14px', marginBottom: 14, fontSize: 12 }}>
                  <div style={{ fontWeight: 700, color: '#F23645', marginBottom: 6 }}>
                    ✅ HOW TO VERIFY OB ON CHART
                  </div>
                  <div style={{ color: '#9B9EA8', lineHeight: 1.6 }}>
                    The OB should be a <strong style={{ color: '#FFF' }}>tight rectangle</strong> sitting
                    at the last opposing candle before the BOS impulse.
                    <br/><span style={{ color: '#F23645' }}>height_atr</span> should be &lt; 1.5.
                    If it's &gt; 2.0, the OB candle was unusually large — still valid but watch for mitigation.
                  </div>
                </div>

                {data['🟥 ORDER BLOCKS']?.length === 0 ? (
                  <div style={{ color: '#666', fontSize: 12, padding: '12px 0' }}>
                    No active OBs found. Either all have been mitigated or no BOS has confirmed yet.
                  </div>
                ) : (
                  data['🟥 ORDER BLOCKS'].map((ob, i) => {
                    const isBear  = ob.type === 'OB_BEAR';
                    const color   = isBear ? '#F23645' : '#26A69A';
                    const heightOk = ob.height_atr <= 1.5;
                    return (
                      <div key={i} style={{ background: '#161A25', border: `1px solid ${color}44`,
                        borderRadius: 6, padding: '10px 14px', marginBottom: 10 }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                            <Badge color={color}>{ob.type}</Badge>
                            <span style={{ fontSize: 12, color: '#FFF', fontWeight: 700 }}>{ob.label}</span>
                          </div>
                          <Badge color={ob.mitigated ? '#666' : color}>
                            {ob.mitigated ? '✗ MITIGATED' : '✓ ACTIVE'}
                          </Badge>
                        </div>
                        <Row label="Zone Top"     value={ob.top}    accent={color} mono />
                        <Row label="Zone Bottom"  value={ob.bottom} accent={color} mono />
                        <Row label="Height (ATR)" value={
                          <span style={{ color: heightOk ? '#26A69A' : '#FFD700' }}>
                            {ob.height_atr}× ATR {heightOk ? '✓ tight' : '⚠ wide'}
                          </span>
                        } />
                        <Row label="Entry Label"  value={ob.entry_label || '—'} accent="#9B9EA8" />
                        <Row label="OB Candle #"  value={ob.candle_idx} mono />
                      </div>
                    );
                  })
                )}
              </div>
            )}

            {/* ── RAW TAB ────────────────────────────────────────────────── */}
            {tab === 'raw' && (
              <div>
                <Section title="Engine Parameters" color="#9B9EA8">
                  <Row label="Instrument"   value={data['📊 INSTRUMENT']} />
                  <Row label="Current Price" value={data['💰 CURRENT PRICE']} mono />
                  <Row label="ATR (14)"      value={data['📏 ATR (14)']} mono accent="#9B9EA8" />
                  <Row label="Swing Order"   value={data['🔍 SWING ORDER']} mono />
                  <Row label="Total Candles" value={data['📈 TOTAL CANDLES']} />
                  {Object.entries(data['📐 PROFILE'] || {}).map(([k, v]) => (
                    <Row key={k} label={`profile.${k}`} value={String(v)} mono />
                  ))}
                </Section>

                <Section title="Recent Swing Highs (candle indices)" color="#F23645">
                  <div style={{ fontFamily: 'monospace', fontSize: 11, color: '#9B9EA8', lineHeight: 2 }}>
                    {(data['raw_swing_highs'] || []).map((idx, i) => (
                      <span key={i} style={{ marginRight: 12 }}>
                        <Badge color="#F23645">#{idx}</Badge>
                      </span>
                    ))}
                  </div>
                </Section>

                <Section title="Recent Swing Lows (candle indices)" color="#26A69A">
                  <div style={{ fontFamily: 'monospace', fontSize: 11, color: '#9B9EA8', lineHeight: 2 }}>
                    {(data['raw_swing_lows'] || []).map((idx, i) => (
                      <span key={i} style={{ marginRight: 12 }}>
                        <Badge color="#26A69A">#{idx}</Badge>
                      </span>
                    ))}
                  </div>
                </Section>

                <Section title="CHoCH Status" color="#FFA726">
                  {data['⚠️  CHoCH']?.detected ? (
                    <div>
                      <Row label="Detected"   value="YES ⚠️"  accent="#FFA726" />
                      <Row label="Level"      value={data['⚠️  CHoCH'].level} mono accent="#FFA726" />
                    </div>
                  ) : (
                    <div style={{ color: '#26A69A', fontSize: 12 }}>✓ No CHoCH — structure is intact</div>
                  )}
                </Section>

                <Section title="Engine Verdict" color="#4B8EFF">
                  <Row label="Waiting For" value={data['─── VERDICT ───']} />
                  <div style={{ marginTop: 8, padding: '8px 10px', background: '#1A1E2A',
                    borderRadius: 4, fontSize: 11, color: '#9B9EA8', fontFamily: 'monospace' }}>
                    {data['📍 WAITING FOR']}
                  </div>
                </Section>
              </div>
            )}

          </div>
        </>
      )}
    </div>
  );
};

export default EngineDebugPanel;
