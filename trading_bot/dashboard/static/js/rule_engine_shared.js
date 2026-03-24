(function () {
    function _toDirectionBias(d) {
        const b = (d.bias && d.bias.bias) || 'NEUTRAL';
        if (b === 'BULLISH') return 'bullish';
        if (b === 'BEARISH') return 'bearish';
        return 'sideways';
    }

    function _toTrendText(d) {
        const b = (d.bias && d.bias.bias) || 'NEUTRAL';
        if (b === 'BULLISH') return 'uptrend';
        if (b === 'BEARISH') return 'downtrend';
        return 'flat/sideways';
    }

    function _vwapTextFromBias(d) {
        const b = (d.bias && d.bias.bias) || 'NEUTRAL';
        if (b === 'BULLISH') return 'above';
        if (b === 'BEARISH') return 'below';
        return 'near';
    }

    function buildRule180Payload(d) {
        const pick = d.recommendation || {};
        const side = d.preferred_side || 'NONE';
        const action = d.action || 'WAIT';
        const sustain = Number(pick.sustain_count || 0);
        const strongMomentum = action === 'BUY' && sustain >= 2;
        const structure = d.bias && d.bias.bias === 'BULLISH'
            ? 'HH/HL'
            : d.bias && d.bias.bias === 'BEARISH'
                ? 'LH/LL'
                : 'none';
        const latestCandleDesc = strongMomentum
            ? 'strong breakout with follow-through'
            : 'indecision / no clear breakout';

        return {
            time: (d.time_state && d.time_state.now) || '',
            pre_market: {
                range_points: Number(d.bias && d.bias.metrics && d.bias.metrics.m1_move_5bars ? Math.abs(d.bias.metrics.m1_move_5bars) * 12 : 0),
                candle_description: strongMomentum ? 'strong body candles' : 'multiple wicks/indecision',
                vwap_position: _vwapTextFromBias(d),
                trend: _toTrendText(d),
                option_premiums: strongMomentum ? 'stable' : 'spiky',
                gap_sr_details: 'unknown',
            },
            entry_confirmation: {
                current_price: Number(d.nifty_spot || 0),
                breakout_level: Number(d.atm || 0),
                vwap_position: _vwapTextFromBias(d),
                market_structure: structure,
                latest_candles: latestCandleDesc,
                volume: strongMomentum ? 'increasing' : 'decreasing',
            },
            premium_180: {
                time: (d.time_state && d.time_state.now) || '',
                ce_premium: side === 'CE' ? Number(pick.ltp || 0) : 0,
                pe_premium: side === 'PE' ? Number(pick.ltp || 0) : 0,
                nifty_direction: _toDirectionBias(d),
                vwap_position: _vwapTextFromBias(d),
                momentum: strongMomentum ? 'strong' : 'weak',
            },
            no_trade: {
                range_points: Number(d.bias && d.bias.metrics && d.bias.metrics.m1_move_5bars ? Math.abs(d.bias.metrics.m1_move_5bars) * 12 : 0),
                candle_structure: strongMomentum ? 'trending' : 'choppy',
                vwap_behavior: strongMomentum ? 'clean trend' : 'frequent touches',
                price_action: strongMomentum ? 'trending' : 'sideways',
                market_structure: structure,
            },
            edge: {
                compression: strongMomentum ? 'yes' : 'no',
                liquidity_sweep: strongMomentum ? 'yes' : 'no',
                reclaim_20ma: strongMomentum ? 'yes' : 'no',
                first_breakout_candle: strongMomentum ? 'yes' : 'no',
                volume_expansion: strongMomentum ? 'yes' : 'no',
                direction: _toDirectionBias(d),
            },
            post_trade: {
                direction: side,
                market_structure: structure,
                indicators: 'VWAP + 20MA',
                result: 'pending',
            },
        };
    }

    function _orbDirection(d) {
        const note = String((d.runtime && d.runtime.note) || '').toLowerCase();
        const recent = (d.recent || [])[0] || {};
        const side = String(recent.side || '').toUpperCase();
        if (side.includes('CE') || note.includes('bull')) return 'bullish';
        if (side.includes('PE') || note.includes('bear')) return 'bearish';
        return 'sideways';
    }

    function _orbVwapProxy(dir) {
        if (dir === 'bullish') return 'above';
        if (dir === 'bearish') return 'below';
        return 'near';
    }

    function buildOrbPayload(d) {
        const rt = d.runtime || {};
        const spot = Number(rt.spot_ltp || 0);
        const orbHigh = Number(rt.orb_high || 0);
        const orbLow = Number(rt.orb_low || 0);
        const range = (orbHigh > 0 && orbLow > 0) ? Math.abs(orbHigh - orbLow) : 0;
        const dir = _orbDirection(d);
        const vwapPos = _orbVwapProxy(dir);
        const breakoutUp = spot > 0 && orbHigh > 0 && spot > orbHigh;
        const breakoutDown = spot > 0 && orbLow > 0 && spot < orbLow;
        const strongBreak = breakoutUp || breakoutDown;
        const structure = dir === 'bullish' ? 'HH/HL' : dir === 'bearish' ? 'LH/LL' : 'none';
        const cePrem = dir === 'bullish' && (d.recent || []).length ? Number((d.recent[0] && d.recent[0].entry) || 0) : 0;
        const pePrem = dir === 'bearish' && (d.recent || []).length ? Number((d.recent[0] && d.recent[0].entry) || 0) : 0;

        return {
            time: String(d.time || ''),
            pre_market: {
                range_points: range,
                candle_description: strongBreak ? 'strong body candles' : 'choppy/indecision',
                vwap_position: vwapPos,
                trend: dir === 'bullish' ? 'uptrend' : dir === 'bearish' ? 'downtrend' : 'flat',
                option_premiums: strongBreak ? 'stable' : 'spiky',
                gap_sr_details: 'unknown',
            },
            entry_confirmation: {
                current_price: spot,
                breakout_level: dir === 'bullish' ? orbHigh : dir === 'bearish' ? orbLow : 0,
                vwap_position: vwapPos,
                market_structure: structure,
                latest_candles: strongBreak ? 'strong breakout with follow-through' : 'weak / fake breakout',
                volume: strongBreak ? 'increasing' : 'decreasing',
            },
            premium_180: {
                time: String(d.time || ''),
                ce_premium: cePrem,
                pe_premium: pePrem,
                nifty_direction: dir,
                vwap_position: vwapPos,
                momentum: strongBreak ? 'strong' : 'weak',
            },
            no_trade: {
                range_points: range,
                candle_structure: strongBreak ? 'trending' : 'choppy',
                vwap_behavior: strongBreak ? 'clean trend' : 'frequent touches',
                price_action: strongBreak ? 'trending' : 'sideways',
                market_structure: structure,
            },
            edge: {
                compression: strongBreak ? 'yes' : 'no',
                liquidity_sweep: strongBreak ? 'yes' : 'no',
                reclaim_20ma: strongBreak ? 'yes' : 'no',
                first_breakout_candle: strongBreak ? 'yes' : 'no',
                volume_expansion: strongBreak ? 'yes' : 'no',
                direction: dir,
            },
            post_trade: {
                direction: dir === 'bullish' ? 'CE' : dir === 'bearish' ? 'PE' : 'NONE',
                market_structure: structure,
                indicators: 'ORB + MA + VWAP proxy',
                result: 'pending',
            },
        };
    }

    async function analyze(payload) {
        const r = await fetch('/api/rule-analysis', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        return r.json();
    }

    function renderResult(d) {
        const final = d.final_summary || {};
        const decision = (final.final_decision || 'NO TRADE').toUpperCase();
        const decisionEl = document.getElementById('engineDecision');
        if (decisionEl) {
            decisionEl.textContent = decision;
            decisionEl.className = 'engine-v ' + (decision === 'TRADE' ? 'trade' : 'no-trade');
        }

        const reasonEl = document.getElementById('engineReason');
        if (reasonEl) reasonEl.textContent = final.reason || '--';

        const metaEl = document.getElementById('engineMeta');
        if (metaEl) {
            metaEl.textContent =
                'Direction: ' + (final.direction || '--') +
                ' | Entry: ' + (final.entry_timing || '--') +
                ' | Confidence: ' + (final.confidence || '--');
        }

        const ga = d.global_alignment || {};
        const alignEl = document.getElementById('engineAlign');
        if (alignEl) {
            alignEl.textContent =
                String(ga.aligned_count || 0) + ' / ' + String(ga.required_minimum || 4);
        }

        const nt = d.no_trade_detection || {};
        const ntEl = document.getElementById('engineNoTrade');
        if (ntEl) {
            ntEl.textContent =
                'No-trade status: ' + (nt.decision || '--') + ' (' + (nt.reason || '--') + ')';
        }

        const en = d.entry_confirmation || {};
        const enEl = document.getElementById('engineEntry');
        if (enEl) {
            enEl.textContent =
                'Entry check: ' + (en.entry || '--') + ' | Confidence: ' + (en.confidence || '--');
        }
    }

    function renderError(msg) {
        var reasonEl = document.getElementById('engineReason');
        if (reasonEl) reasonEl.textContent = msg || 'Rule engine API unavailable';
    }

    async function run(payload) {
        const d = await analyze(payload);
        if (d && !d.error) {
            renderResult(d);
        } else {
            renderError((d && d.error) ? String(d.error) : 'Rule engine API unavailable');
        }
        return d;
    }

    window.RuleEngineShared = {
        buildRule180Payload: buildRule180Payload,
        buildOrbPayload: buildOrbPayload,
        analyze: analyze,
        renderResult: renderResult,
        renderError: renderError,
        run: run,
    };
})();
