import threading
import logging
import csv
import os
import time
import schedule
from datetime import datetime, timedelta, date
from datetime import time as time2
import alpaca_trade_api as tradeapi
import pytz
import numpy as np
from collections import deque
import talib
import yfinance as yf
import sqlalchemy
from sqlalchemy import create_engine, Column, Integer, String, Float, event
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.exc import SQLAlchemyError
from ratelimit import limits, sleep_and_retry
import pandas_market_calendars as mcal

# =================================================================================
# ---------------- ML BRAIN (Kalshi TFBrain-style, inlined) ----------------------
# =================================================================================
# Adapted from the Kalshi day-trading robot's TFBrain architecture, with the
# per-symbol / per-coin / per-ETF-fund brains INTENTIONALLY removed per instruction:
# there is exactly ONE brain here, shared across every symbol the bot trades. All
# training data comes from yfinance -- no crypto exchange feeds, no per-asset
# specialization.
#
# ARCHITECTURE (matches Kalshi's TFBrain shape):
#     Input: (batch, ML_BRAIN_SEQ_LEN=20, ML_BRAIN_FEATURES) -- a rolling window
#            of the last 20 daily feature snapshots ending at "now".
#          |
#     Conv1D(64, kernel=3, causal) -> BatchNorm -> Dropout(0.25)
#          |  captures short-term local patterns (3-day motifs)
#     LSTM(128, return_sequences=True) -> Dropout(0.25)
#          |  learns temporal dependencies across the full 20-day window
#     LSTM(64) -> Dropout(0.20)
#          |  compresses the sequence into a fixed-length context vector
#     Dense(32, relu) -> Dense(1, sigmoid) = "will this trade be profitable?"
#
# TRAINING SIGNAL (also from Kalshi):
#   - Focal loss: concentrates gradient on trades the model is currently
#     getting WRONG, instead of spending capacity on already-confident-correct
#     examples ("extreme desire to win" expressed as a loss function).
#   - Per-sample loss penalty: losers weighted heavier than winners.
#   - Win-probability threshold at inference time: only trades when confident.
#
# TRAINING SCHEDULE (per instruction):
#   - First run (on-startup, if no model exists yet OR forced): 2,500 examples,
#     lightweight -- enough to seed a working model without overfitting on
#     limited data.
#   - Every day at 17:00 ET: full training run at 15,000 examples. Must
#     complete before 07:45 ET the next morning so it doesn't collide with the
#     08:00-ish trading day starting up (leaves ~14.5 hours of overnight
#     runway, which is plenty for a 15k-example run on this small model).
#
# NO Runpod, NO websocket feed, NO web server, NO per-symbol brains, NO
# per-ETF brains, NO crypto price sources. Everything runs in-process using
# yfinance for all historical data.
# =================================================================================

import json as _ml_json
from collections import deque as _ml_deque

_ml_base_dir = os.path.dirname(os.path.abspath(__file__))
ML_BRAIN_DIR = os.path.join(_ml_base_dir, 'ml_brain_model')
ML_MODEL_PATH = os.path.join(ML_BRAIN_DIR, 'model.keras')
ML_META_PATH = os.path.join(ML_BRAIN_DIR, 'meta.json')
ML_STATE_PATH = os.path.join(ML_BRAIN_DIR, 'schedule_state.json')

# Kept for external status callers -- points at the same meta file the new
# code writes to, so get_ml_status() etc. keep working.
ML_WEIGHTS_PATH = ML_MODEL_PATH
ML_MODEL_DIR = ML_BRAIN_DIR

# ---- Model hyperparameters (mirror Kalshi TFBrain constants, tuned for the
# smaller daily-bar feature vector we build below) ----
ML_BRAIN_SEQ_LEN = 20              # rolling window of 20 daily bars
ML_BRAIN_FEATURES = 10             # per-day feature count: see _ml_build_feature_row()
ML_BRAIN_LEARNING_RATE = 0.0006
ML_BRAIN_FOCAL_GAMMA = 1.2         # focal loss: focus on hard examples
ML_BRAIN_LOSS_PENALTY = 1.2        # losers weighted 1.2x winners
ML_BRAIN_BATCH_SIZE = 64
ML_BRAIN_WIN_THRESHOLD = 0.55      # inference: only nudge score up when P(win) >= this

# ---- Training-schedule config (per instruction) ----
ML_FIRST_RUN_EXAMPLES = 2500       # midpoint of 1,000-5,000
ML_DAILY_RUN_EXAMPLES = 15000      # midpoint of 10,000-20,000
# Hard lifetime cap on HISTORICAL PRETRAINING examples (per instruction).
# Once cumulative n_updates reaches this, historical pretraining stops
# entirely and only daily MAINTENANCE training on live win/loss outcomes
# runs from that point forward. The cap is on cumulative pretraining
# examples across all prior runs, tracked in meta.json's n_updates field.
ML_PRETRAIN_LIFETIME_CAP = 20000
# Once pretraining is capped, the daily maintenance pass fine-tunes the
# model on the last MAINTENANCE_LOOKBACK_DAYS of closed live trades.
ML_MAINTENANCE_LOOKBACK_DAYS = 1
ML_MAINTENANCE_MIN_TRADES = 5   # skip maintenance run if fewer live trades closed than this
ML_DAILY_TRAIN_HOUR = 17           # 5:00 PM ET start
ML_DAILY_TRAIN_MUST_FINISH_HOUR = 7    # done before 7:45 AM ET next day
ML_DAILY_TRAIN_MUST_FINISH_MINUTE = 45
ML_HIST_LOOKBACK_YEARS = 2
ML_HIST_FORWARD_LABEL_DAYS = 5      # label = 1 if close[N+5] > close[N]
ML_HIST_MIN_ROWS_PER_SYMBOL = ML_BRAIN_SEQ_LEN + ML_HIST_FORWARD_LABEL_DAYS + 40

# ---- Live-inference guardrails, unchanged in spirit from the prior version ----
ML_MIN_TRADES_FOR_LIVE_ADJUSTMENT = 60  # gate on LIVE trades before we adjust LIVE decisions
ML_MAX_SCORE_ADJUSTMENT = 1.5           # cap on how many buy-score points the model can add/subtract

_ml_lock = threading.RLock()  # RLock so nested calls (e.g. train -> load_cached_model) don't self-deadlock
_ml_model_cache = {'model': None, 'trained_at': None, 'n_updates': 0}
_ml_tf_availability_cache = {'checked': False, 'available': False}


def _ml_lazy_import_tf():
    """
    TensorFlow is a heavy, optional dependency. Import it lazily and let any
    failure (not installed, wrong platform, etc.) disable the ML signal
    gracefully rather than crashing the live trading bot at startup.
    """
    try:
        import tensorflow as tf
        return tf
    except Exception as e:
        logging.warning(f"ml_brain: TensorFlow unavailable ({e}); ML scoring disabled.")
        return None


ML_BRAIN_AVAILABLE = False   # set on first _ml_brain_is_available() call


def _ml_brain_is_available():
    """
    Lazily resolves and caches TensorFlow availability on first call.
    TensorFlow's import alone can take 30-60 seconds -- doing it eagerly at
    bot.py startup would badly delay position reconciliation and the first
    trading cycle.
    """
    global ML_BRAIN_AVAILABLE
    if not _ml_tf_availability_cache['checked']:
        _ml_tf_availability_cache['available'] = _ml_lazy_import_tf() is not None
        _ml_tf_availability_cache['checked'] = True
        ML_BRAIN_AVAILABLE = _ml_tf_availability_cache['available']
        if not _ml_tf_availability_cache['available']:
            print("ML brain: TensorFlow unavailable; buy scoring will run without the ML adjustment.")
    return _ml_tf_availability_cache['available']


def _ml_make_focal_loss(gamma):
    """Kalshi's focal loss, adapted:
    gamma=0 reduces to ordinary binary cross-entropy. Higher gamma => sharper
    focus on hard (currently-misclassified) examples. Works alongside the
    per-sample weight multiplication for the loss-penalty term.
    """
    def focal(y_true, y_pred):
        import tensorflow as _tf
        eps = _tf.keras.backend.epsilon()
        y_pred = _tf.clip_by_value(y_pred, eps, 1.0 - eps)
        y_true = _tf.cast(y_true, y_pred.dtype)
        p_t = y_true * y_pred + (1.0 - y_true) * (1.0 - y_pred)
        ce = -_tf.math.log(p_t)
        focal_factor = _tf.pow(1.0 - p_t, gamma)
        return focal_factor * ce
    focal.__name__ = "focal_loss"
    return focal


def _ml_build_model(tf):
    """Build the Kalshi-style Conv1D -> LSTM -> LSTM -> Dense sequence model.
    Single model, shared across all symbols the bot trades -- no per-symbol
    or per-ETF brains, per instruction. Called fresh whenever a model is
    needed and no saved one exists yet."""
    from tensorflow import keras
    from tensorflow.keras import layers

    inputs = keras.Input(shape=(ML_BRAIN_SEQ_LEN, ML_BRAIN_FEATURES), name='features_seq')
    x = layers.Conv1D(64, kernel_size=3, padding='causal', activation='relu',
                      kernel_regularizer=keras.regularizers.l2(1e-4))(inputs)
    x = layers.BatchNormalization()(x)
    x = layers.Dropout(0.25)(x)
    x = layers.LSTM(128, return_sequences=True,
                    kernel_regularizer=keras.regularizers.l2(1e-4))(x)
    x = layers.Dropout(0.25)(x)
    x = layers.LSTM(64)(x)
    x = layers.Dropout(0.20)(x)
    x = layers.Dense(32, activation='relu')(x)
    outputs = layers.Dense(1, activation='sigmoid', name='win_prob')(x)

    model = keras.Model(inputs, outputs, name='ml_brain')
    model.compile(optimizer=keras.optimizers.Adam(learning_rate=ML_BRAIN_LEARNING_RATE),
                  loss=_ml_make_focal_loss(ML_BRAIN_FOCAL_GAMMA),
                  metrics=['accuracy'])
    return model


def _ml_build_feature_row(rsi_val, macd_val, macd_sig_val, atr_val, close_val,
                          volume_val, vol_sma_val, close_sma20, close_sma50,
                          bar_return):
    """One day's feature snapshot (10 features), used both when generating
    historical training rows and (eventually) when scoring at inference time.
    Kept intentionally small so the model is fast to train even at the
    15k-example daily budget."""
    def _fin(x, default=0.0):
        try:
            v = float(x)
            return v if np.isfinite(v) else default
        except (TypeError, ValueError):
            return default

    close = _fin(close_val, 1.0)
    return [
        _fin(rsi_val, 50.0) / 100.0,                           # 0: RSI normalized [0,1]
        _fin(macd_val, 0.0),                                    # 1: MACD raw
        _fin(macd_sig_val, 0.0),                                # 2: MACD signal raw
        1.0 if _fin(macd_val) > _fin(macd_sig_val) else 0.0,   # 3: MACD-above-signal indicator
        _fin(atr_val, 0.0) / max(close, 0.01),                 # 4: ATR%
        _fin(bar_return, 0.0),                                  # 5: day's return
        (_fin(volume_val, 0.0) / max(_fin(vol_sma_val, 1.0), 1.0)) - 1.0,  # 6: volume vs SMA
        (close / max(_fin(close_sma20, close), 0.01)) - 1.0,   # 7: distance from 20-SMA
        (close / max(_fin(close_sma50, close), 0.01)) - 1.0,   # 8: distance from 50-SMA
        1.0 if _fin(close_sma20, 0) > _fin(close_sma50, 0) else 0.0,  # 9: short-trend > long-trend
    ]


def _ml_build_examples_for_symbol(symbol, df, n_examples_wanted):
    """Turns one symbol's daily OHLCV history into (sequence, label) training
    pairs -- a sequence is ML_BRAIN_SEQ_LEN=20 consecutive daily feature rows
    ending at day N; label is 1 if close[N + ML_HIST_FORWARD_LABEL_DAYS] >
    close[N], else 0. Only anchor points where all indicators are valid are
    used. Caller specifies how many examples they want to try to draw; if the
    symbol doesn't have that many valid anchor points, returns fewer.
    """
    if df is None or df.empty or len(df) < ML_HIST_MIN_ROWS_PER_SYMBOL:
        return []

    close = df['Close'].values.astype(np.float64)
    high = df['High'].values.astype(np.float64)
    low = df['Low'].values.astype(np.float64)
    volume = df['Volume'].values.astype(np.float64) if 'Volume' in df else np.zeros(len(close))

    if len(close) < ML_HIST_MIN_ROWS_PER_SYMBOL or np.any(np.isnan(close)):
        return []

    try:
        rsi = talib.RSI(close, timeperiod=14)
        macd, macd_signal, _ = talib.MACD(close, fastperiod=12, slowperiod=26, signalperiod=9)
        atr = talib.ATR(high, low, close, timeperiod=14)
        vol_sma = talib.SMA(volume, timeperiod=14) if volume.sum() > 0 else np.full(len(close), 1.0)
        sma20 = talib.SMA(close, timeperiod=20)
        sma50 = talib.SMA(close, timeperiod=50)
    except Exception as e:
        logging.warning(f"ml_brain hist: indicator calc failed for {symbol}: {e}")
        return []

    daily_return = np.diff(close, prepend=close[0]) / np.maximum(close, 0.01)

    # Pre-build every day's feature row once, then slice sequences from it.
    n_days = len(close)
    feature_rows = np.zeros((n_days, ML_BRAIN_FEATURES), dtype=np.float32)
    for i in range(n_days):
        feature_rows[i] = _ml_build_feature_row(
            rsi[i], macd[i], macd_signal[i], atr[i], close[i],
            volume[i], vol_sma[i], sma20[i], sma50[i], daily_return[i])

    # Valid anchor points: at least SEQ_LEN days of history behind and
    # FORWARD_LABEL_DAYS days of future ahead, all indicators non-NaN.
    first_valid = 50  # after SMA50 stabilizes
    last_valid = n_days - ML_HIST_FORWARD_LABEL_DAYS - 1
    if last_valid <= first_valid:
        return []

    candidate_anchors = []
    for i in range(max(first_valid, ML_BRAIN_SEQ_LEN), last_valid + 1):
        if (not np.isnan(rsi[i]) and not np.isnan(macd[i]) and
            not np.isnan(atr[i]) and not np.isnan(sma50[i])):
            candidate_anchors.append(i)

    if not candidate_anchors:
        return []

    # Sample uniformly across the symbol's history -- avoids over-weighting
    # any particular regime the symbol happened to spend a lot of time in.
    rng = np.random.default_rng(hash(symbol) & 0xFFFFFFFF)
    if len(candidate_anchors) > n_examples_wanted:
        picked = rng.choice(candidate_anchors, n_examples_wanted, replace=False)
    else:
        picked = candidate_anchors

    examples = []
    for i in picked:
        seq = feature_rows[i - ML_BRAIN_SEQ_LEN + 1 : i + 1]
        if seq.shape != (ML_BRAIN_SEQ_LEN, ML_BRAIN_FEATURES):
            continue
        future_close = close[i + ML_HIST_FORWARD_LABEL_DAYS]
        label = 1.0 if future_close > close[i] else 0.0
        examples.append((seq.astype(np.float32), float(label)))
    return examples


def _ml_hist_fetch_symbol_universe():
    """Uses the SAME candidate list file the bot itself trades from, so the
    model trains on symbols the bot will actually see. Reads
    electricity-or-utility-stocks-to-buy-list.txt, same file
    performance-stock-list-writer.py writes and get_symbols_to_buy() reads."""
    try:
        with open('electricity-or-utility-stocks-to-buy-list.txt', 'r') as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        return []


def _ml_gather_training_examples(target_count):
    """Downloads the current candidate-list universe's historical data
    (yfinance, batched to respect the shared rate limiter) and builds up to
    `target_count` (sequence, label) examples across all symbols.

    Returns (examples_list, symbols_used, symbols_attempted).
    """
    symbols = _ml_hist_fetch_symbol_universe()
    if not symbols:
        return [], 0, 0

    end_date = datetime.now(eastern).date()
    start_date = end_date - timedelta(days=int(365 * ML_HIST_LOOKBACK_YEARS))

    try:
        data_by_symbol = yf_download_batch(symbols, start=start_date.isoformat(),
                                           end=end_date.isoformat(), interval='1d')
    except Exception as e:
        logging.error(f"ml_brain: batch download failed: {e}")
        return [], 0, len(symbols)

    if not data_by_symbol:
        return [], 0, len(symbols)

    # Even split of the target budget across symbols that returned data --
    # prevents any single very-long-history symbol from dominating.
    per_symbol_budget = max(20, target_count // max(1, len(data_by_symbol)))

    all_examples = []
    symbols_used = 0
    for sym, df in data_by_symbol.items():
        examples = _ml_build_examples_for_symbol(sym, df, per_symbol_budget)
        if examples:
            symbols_used += 1
            all_examples.extend(examples)
        if len(all_examples) >= target_count:
            break

    # If we're way over target, trim; if under, take what we got.
    if len(all_examples) > target_count:
        rng = np.random.default_rng(0)
        picks = rng.choice(len(all_examples), target_count, replace=False)
        all_examples = [all_examples[i] for i in picks]

    return all_examples, symbols_used, len(symbols)


def _ml_train_on_examples(examples, run_kind):
    """Actually runs model.fit() on a list of (sequence, label) examples.
    Kalshi-style: focal loss + per-sample loss penalty + class-balance
    weighting so a wrong forecast on a "loser" costs the model more than a
    wrong forecast on a "winner". Saves the trained model + meta.json under
    ML_BRAIN_DIR when done.
    """
    tf = _ml_lazy_import_tf()
    if tf is None:
        return "tensorflow unavailable; skipped"

    if not examples:
        return "no examples supplied; skipped"

    from tensorflow import keras

    X = np.stack([e[0] for e in examples]).astype(np.float32)
    y = np.array([e[1] for e in examples], dtype=np.float32)

    n_pos = max(1, int(y.sum()))
    n_neg = max(1, len(y) - n_pos)
    w_pos = len(y) / (2.0 * n_pos)
    w_neg = len(y) / (2.0 * n_neg)
    sample_weight = np.where(y > 0.5, w_pos, w_neg * ML_BRAIN_LOSS_PENALTY).astype(np.float32)

    n_val = max(20, int(len(X) * 0.15))
    # Chronological ordering isn't meaningful here (examples span many
    # symbols and dates); random shuffle before split is the right choice.
    rng = np.random.default_rng(42)
    idx = rng.permutation(len(X))
    X, y, sw = X[idx], y[idx], sample_weight[idx]
    X_train, X_val = X[:-n_val], X[-n_val:]
    y_train, y_val = y[:-n_val], y[-n_val:]
    sw_train = sw[:-n_val]

    with _ml_lock:
        model = _ml_load_cached_model(tf) or _ml_build_model(tf)
        epochs = 6 if run_kind == 'first_run' else 12
        try:
            history = model.fit(X_train, y_train,
                                epochs=epochs,
                                batch_size=min(ML_BRAIN_BATCH_SIZE, len(X_train)),
                                sample_weight=sw_train,
                                validation_data=(X_val, y_val),
                                verbose=0, shuffle=True)
        except Exception as e:
            logging.error(f"ml_brain: model.fit failed: {e}")
            return f"model.fit failed: {e}"

        val_acc = history.history.get('val_accuracy', [None])[-1]
        val_loss = history.history.get('val_loss', [None])[-1]
        train_loss = history.history.get('loss', [None])[-1]

        os.makedirs(ML_BRAIN_DIR, exist_ok=True)
        try:
            model.save(ML_MODEL_PATH)
        except Exception as e:
            logging.error(f"ml_brain: model.save failed: {e}")
            return f"model.save failed: {e}"

        meta = {
            'trained_at': datetime.utcnow().isoformat(),
            'trained_from': f'historical_yfinance ({run_kind})',
            'n_examples': len(X),
            'n_train': int(len(X_train)),
            'n_val': int(len(X_val)),
            'val_accuracy': float(val_acc) if val_acc is not None else None,
            'val_loss': float(val_loss) if val_loss is not None else None,
            'train_loss': float(train_loss) if train_loss is not None else None,
            'n_updates': _ml_model_cache.get('n_updates', 0) + len(X),
        }
        with open(ML_META_PATH, 'w') as f:
            _ml_json.dump(meta, f, indent=2)

        _ml_model_cache['model'] = model
        _ml_model_cache['trained_at'] = meta['trained_at']
        _ml_model_cache['n_updates'] = meta['n_updates']

        return (f"[{run_kind}] trained on {len(X_train)} sequences, validated on {len(X_val)} "
                f"(val_accuracy={val_acc:.3f}, val_loss={val_loss:.3f}, "
                f"train_loss={train_loss:.3f})" if val_acc is not None else
                f"[{run_kind}] trained on {len(X_train)} sequences, validated on {len(X_val)}")


def _ml_load_cached_model(tf):
    """Load the most recently saved model from disk, if not already in the
    process cache. Returns None if nothing has been saved yet or if load
    fails (caller falls back to _ml_build_model()).
    """
    with _ml_lock:
        if _ml_model_cache['model'] is not None:
            return _ml_model_cache['model']
        if not os.path.exists(ML_MODEL_PATH):
            return None
        try:
            from tensorflow import keras
            # custom_objects lets Keras deserialize the focal loss function
            # by name when re-loading a saved model.
            model = keras.models.load_model(
                ML_MODEL_PATH,
                custom_objects={'focal_loss': _ml_make_focal_loss(ML_BRAIN_FOCAL_GAMMA)},
                compile=False,
            )
            model.compile(optimizer=keras.optimizers.Adam(learning_rate=ML_BRAIN_LEARNING_RATE),
                          loss=_ml_make_focal_loss(ML_BRAIN_FOCAL_GAMMA),
                          metrics=['accuracy'])
            _ml_model_cache['model'] = model
            if os.path.exists(ML_META_PATH):
                with open(ML_META_PATH) as f:
                    meta = _ml_json.load(f)
                _ml_model_cache['trained_at'] = meta.get('trained_at')
                _ml_model_cache['n_updates'] = meta.get('n_updates', 0)
            return model
        except Exception as e:
            logging.warning(f"ml_brain: failed to load saved model ({e}); will rebuild.")
            return None


# =================================================================================
# Training schedule + entry points
# =================================================================================

def _ml_load_schedule_state():
    try:
        if os.path.exists(ML_STATE_PATH):
            with open(ML_STATE_PATH) as f:
                return _ml_json.load(f)
    except Exception:
        pass
    return {}


def _ml_save_schedule_state(state):
    try:
        os.makedirs(ML_BRAIN_DIR, exist_ok=True)
        with open(ML_STATE_PATH, 'w') as f:
            _ml_json.dump(state, f, indent=2)
    except Exception as e:
        logging.warning(f"ml_brain: failed to save schedule state ({e}).")


def run_ml_first_training_if_needed():
    """First-run training: fires ONCE, the first time this bot ever calls
    maybe_run_scheduled_ml_training(), if no model exists on disk yet. Uses
    ML_FIRST_RUN_EXAMPLES (2,500) -- lighter than the daily budget so the bot
    has a working model as soon as possible instead of waiting for the first
    17:00 slot. Returns a status string if it actually ran, None otherwise.
    """
    if os.path.exists(ML_MODEL_PATH):
        return None  # already have a model from a prior day

    state = _ml_load_schedule_state()
    if state.get('first_run_completed'):
        return None

    if not _ml_brain_is_available():
        return None

    print(f"ML brain: no existing model on disk. Running first-time training "
          f"({ML_FIRST_RUN_EXAMPLES} examples).")
    logging.info(f"ml_brain: first-time training starting ({ML_FIRST_RUN_EXAMPLES} examples).")

    first_budget = min(ML_FIRST_RUN_EXAMPLES, ML_PRETRAIN_LIFETIME_CAP)
    examples, symbols_used, symbols_attempted = _ml_gather_training_examples(first_budget)
    if len(examples) < 100:
        msg = (f"first-time training: only gathered {len(examples)} examples from "
              f"{symbols_used}/{symbols_attempted} symbols (need 100+); "
              f"deferring to next scheduled run.")
        logging.warning(f"ml_brain: {msg}")
        return msg

    status = _ml_train_on_examples(examples, run_kind='first_run')

    state['first_run_completed'] = True
    state['first_run_completed_at'] = datetime.now(eastern).isoformat()
    _ml_save_schedule_state(state)

    return f"first-time training ({len(examples)} examples from {symbols_used} symbols): {status}"


def _ml_in_daily_train_window(now):
    """True from 17:00 ET (today) through 07:45 ET (next day), i.e. the
    off-hours window in which the daily 15k-example run must both START and
    FINISH. The intent per instruction is 'begin at 17:00 ET, finish before
    07:45 ET' -- this returns True as long as we're inside that overnight
    window, so a bot that started up during it can still catch the run.
    """
    hour, minute = now.hour, now.minute
    # After 17:00 through end of day
    if hour >= ML_DAILY_TRAIN_HOUR:
        return True
    # Or before 07:45 the next morning
    if hour < ML_DAILY_TRAIN_MUST_FINISH_HOUR:
        return True
    if hour == ML_DAILY_TRAIN_MUST_FINISH_HOUR and minute < ML_DAILY_TRAIN_MUST_FINISH_MINUTE:
        return True
    return False


def _ml_daily_train_key_for(now):
    """
    Runs are identified by the DATE OF THE 17:00 START, not the calendar day
    the training might spill into. A run that starts at 23:00 on Tuesday and
    a run that starts at 01:00 on Wednesday morning are the SAME logical
    "Tuesday overnight" run and shouldn't both fire.
    """
    if now.hour >= ML_DAILY_TRAIN_HOUR:
        return now.date().isoformat()
    # It's between midnight and 07:45 -- attribute to yesterday's window.
    return (now.date() - timedelta(days=1)).isoformat()


def _ml_maintenance_train_on_live_trades(sess, tf_model):
    """Post-cap daily maintenance: once cumulative pretraining has hit
    ML_PRETRAIN_LIFETIME_CAP, historical pretraining stops entirely. In
    its place, each scheduled 17:00 ET window fine-tunes the model on
    the last ML_MAINTENANCE_LOOKBACK_DAYS of the bot's OWN closed live
    trades -- literal 'win/loss of the market for that 24 hour time' per
    instruction.

    Sequences are rebuilt from each trade's symbol using yfinance, ending
    at the trade's entry date, labeled by whether the trade actually won
    (outcome_pct > 0). No new example count is added to n_updates -- the
    lifetime cap is on PRETRAINING, not on maintenance -- so maintenance
    passes can keep running indefinitely without ever re-opening the
    pretraining pipeline.
    """
    if not _ml_brain_is_available():
        return "tensorflow unavailable; maintenance skipped"

    cutoff = datetime.now(eastern) - timedelta(days=ML_MAINTENANCE_LOOKBACK_DAYS)
    try:
        rows = (sess.query(tf_model)
                .filter(tf_model.outcome_pct.isnot(None))
                .filter(tf_model.buy_score.isnot(None))
                .all())
        # Filter to recent by exit_date if the field exists on the row
        recent = []
        for r in rows:
            exit_date_str = getattr(r, 'exit_date', None)
            if not exit_date_str:
                continue
            try:
                exit_dt = datetime.fromisoformat(exit_date_str).replace(tzinfo=eastern) \
                    if 'T' in exit_date_str else \
                    datetime.strptime(exit_date_str, '%Y-%m-%d').replace(tzinfo=eastern)
            except Exception:
                continue
            if exit_dt >= cutoff:
                recent.append(r)
    except Exception as e:
        return f"maintenance: live-trade query failed ({e}); skipping"

    if len(recent) < ML_MAINTENANCE_MIN_TRADES:
        return (f"maintenance: only {len(recent)} live trades closed in the "
               f"last {ML_MAINTENANCE_LOOKBACK_DAYS} day(s) (need "
               f"{ML_MAINTENANCE_MIN_TRADES}+); skipping this window.")

    # Build one training sequence per closed trade using its symbol's
    # daily history ending AT (or just before) the trade's entry date.
    # This is much cheaper than a batch pretraining pull.
    examples = []
    for r in recent:
        symbol = getattr(r, 'symbols', None) or getattr(r, 'symbol', None)
        entry_date_str = getattr(r, 'entry_date', None)
        if not symbol or not entry_date_str:
            continue
        try:
            entry_dt = datetime.fromisoformat(entry_date_str) \
                if 'T' in entry_date_str else \
                datetime.strptime(entry_date_str, '%Y-%m-%d')
        except Exception:
            continue
        try:
            start = (entry_dt - timedelta(days=90)).date().isoformat()
            end = (entry_dt + timedelta(days=1)).date().isoformat()
            df_map = yf_download_batch([symbol], start=start, end=end, interval='1d')
        except Exception:
            continue
        df = df_map.get(symbol)
        if df is None or df.empty or len(df) < ML_BRAIN_SEQ_LEN + 5:
            continue
        seq = _ml_current_feature_row_for_symbol(symbol, df, None, None,
                                                  None, None, None, None)
        if seq is None:
            continue
        label = 1.0 if float(r.outcome_pct) > 0 else 0.0
        examples.append((seq, label))

    if len(examples) < ML_MAINTENANCE_MIN_TRADES:
        return (f"maintenance: could only rebuild {len(examples)} usable sequences "
               f"from {len(recent)} closed trades; skipping.")

    return _ml_train_on_examples(examples, run_kind='maintenance')


def maybe_run_scheduled_ml_training():
    """Called every cycle from the main loop. Runs the first-time bootstrap
    if a model doesn't exist yet, then otherwise checks whether we're inside
    a daily-training window that hasn't fired yet. Returns a status string
    when it actually did something, None otherwise (so the caller only logs
    on real activity).

    Cheap to call every cycle: file-existence check + a small JSON read.
    """
    if not _ml_brain_is_available():
        return None

    # First-time bootstrap has priority. If a model already exists on disk,
    # this returns None immediately.
    first = run_ml_first_training_if_needed()
    if first is not None:
        return first

    now = datetime.now(eastern)
    if not _ml_in_daily_train_window(now):
        return None

    state = _ml_load_schedule_state()
    last_daily_key = state.get('last_daily_run_window_key')
    todays_key = _ml_daily_train_key_for(now)
    if last_daily_key == todays_key:
        return None  # already fired for this window

    # Claim the slot BEFORE the slow download starts, so a slow run doesn't
    # cause a second thread on the next tick to see 'not yet fired' and
    # start a duplicate training pass.
    state['last_daily_run_window_key'] = todays_key
    state['last_daily_run_started_at'] = now.isoformat()
    _ml_save_schedule_state(state)

    # Lifetime pretraining cap check (per instruction: no brain model
    # should train more than 20,000 times for pre-training, then just
    # daily maintenance training on the win/loss of the market for that
    # 24-hour time). n_updates in meta.json is the cumulative count of
    # PRETRAINING examples across every historical run this brain has
    # ever seen; once it clears ML_PRETRAIN_LIFETIME_CAP, we permanently
    # switch to daily maintenance on live win/loss outcomes only.
    lifetime_n = _ml_model_cache.get('n_updates', 0)
    if lifetime_n == 0 and os.path.exists(ML_META_PATH):
        try:
            with open(ML_META_PATH) as f:
                lifetime_n = int(_ml_json.load(f).get('n_updates', 0))
        except Exception:
            lifetime_n = 0

    if lifetime_n >= ML_PRETRAIN_LIFETIME_CAP:
        print(f"ML brain: cumulative pretraining n_updates={lifetime_n} has reached "
              f"the lifetime cap of {ML_PRETRAIN_LIFETIME_CAP}. Switching to daily "
              f"MAINTENANCE training on live win/loss only.")
        logging.info(f"ml_brain: maintenance mode (lifetime cap {ML_PRETRAIN_LIFETIME_CAP} reached).")
        status = _ml_maintenance_train_on_live_trades(session, TradeFeatures)
        state['last_daily_run_completed_at'] = datetime.now(eastern).isoformat()
        _ml_save_schedule_state(state)
        return status

    # Cap the request size so a single run can't blow past the lifetime
    # limit -- if we're 3,000 away from the cap and someone set the
    # daily budget to 15,000, only pull 3,000 this time.
    daily_budget = min(ML_DAILY_RUN_EXAMPLES, ML_PRETRAIN_LIFETIME_CAP - lifetime_n)
    print(f"ML brain: daily training window (17:00-07:45 ET), starting "
          f"{daily_budget}-example run (lifetime n_updates so far: {lifetime_n}).")
    logging.info(f"ml_brain: daily training starting ({daily_budget} examples, "
                f"lifetime n_updates={lifetime_n}).")

    examples, symbols_used, symbols_attempted = _ml_gather_training_examples(daily_budget)
    if len(examples) < 500:
        msg = (f"daily training: only gathered {len(examples)} examples from "
              f"{symbols_used}/{symbols_attempted} symbols (need 500+); skipping this window.")
        logging.warning(f"ml_brain: {msg}")
        return msg

    status = _ml_train_on_examples(examples, run_kind='daily')

    state['last_daily_run_completed_at'] = datetime.now(eastern).isoformat()
    _ml_save_schedule_state(state)

    return f"daily training ({len(examples)} examples from {symbols_used} symbols): {status}"


# =================================================================================
# Live-inference adjustment (called from buy_stocks)
# =================================================================================

def _ml_current_feature_row_for_symbol(symbol, df, rsi_val, macd_val,
                                        macd_sig_val, atr_val, volume_val,
                                        current_price):
    """Build a live feature row from the latest bar of `df` (which
    compute_buy_score already has in hand). Used by get_ml_adjustment() to
    turn the current market state into a sequence input.

    Returns None if there's not enough recent data to build a full ML_BRAIN_SEQ_LEN
    window -- caller should fall back to no adjustment.
    """
    if df is None or df.empty or len(df) < ML_BRAIN_SEQ_LEN + 5:
        return None

    close = df['Close'].values.astype(np.float64)
    high = df['High'].values.astype(np.float64)
    low = df['Low'].values.astype(np.float64)
    volume = df['Volume'].values.astype(np.float64) if 'Volume' in df else np.zeros(len(close))

    try:
        rsi = talib.RSI(close, timeperiod=14)
        macd, macd_signal, _ = talib.MACD(close, fastperiod=12, slowperiod=26, signalperiod=9)
        atr_series = talib.ATR(high, low, close, timeperiod=14)
        vol_sma = talib.SMA(volume, timeperiod=14) if volume.sum() > 0 else np.full(len(close), 1.0)
        sma20 = talib.SMA(close, timeperiod=20)
        sma50 = talib.SMA(close, timeperiod=50)
    except Exception:
        return None

    daily_return = np.diff(close, prepend=close[0]) / np.maximum(close, 0.01)

    n_needed = ML_BRAIN_SEQ_LEN
    start = len(close) - n_needed
    if start < 50:
        return None

    seq = np.zeros((n_needed, ML_BRAIN_FEATURES), dtype=np.float32)
    for j, i in enumerate(range(start, start + n_needed)):
        if np.isnan(rsi[i]) or np.isnan(macd[i]) or np.isnan(atr_series[i]):
            return None
        seq[j] = _ml_build_feature_row(
            rsi[i], macd[i], macd_signal[i], atr_series[i], close[i],
            volume[i], vol_sma[i], sma20[i], sma50[i], daily_return[i])
    return seq


def get_ml_adjustment(sess, tf_model, buy_score=None, rsi=None, atr_pct=None,
                      macd_above_signal=None, volume_holding=None, regime=None,
                      symbol=None, df=None, current_price=None):
    """Returns a small buy-score adjustment (float, can be negative) or None
    if the model isn't trained/eligible yet. Callers must treat None as "no
    opinion" and fall back entirely to the rule-based score.

    Signature preserved from the previous ML wiring so buy_stocks doesn't
    need changing. The new sequence-based model wants a rolling window of
    daily bars, so it looks at `df` (which buy_stocks already fetches via
    get_cached_data + yf_history for compute_buy_score) to build the sequence.
    If `df` isn't supplied (older callers) or is too short, returns None.
    """
    if not _ml_brain_is_available():
        return None

    # LIVE-trust gate: even a well-trained historical model shouldn't touch
    # LIVE decisions until this bot has enough of its own outcomes on record
    # for the operator to have some basis for trusting it. Same philosophy
    # AdaptiveParams uses elsewhere in this file.
    try:
        live_rows = (sess.query(tf_model)
                     .filter(tf_model.outcome_pct.isnot(None))
                     .filter(tf_model.buy_score.isnot(None))
                     .all())
        if len(live_rows) < ML_MIN_TRADES_FOR_LIVE_ADJUSTMENT:
            return None
    except Exception as e:
        logging.warning(f"ml_brain: live-trades gate query failed ({e}); no adjustment.")
        return None

    tf = _ml_lazy_import_tf()
    if tf is None:
        return None

    model = _ml_load_cached_model(tf)
    if model is None:
        return None

    if df is None:
        return None
    seq = _ml_current_feature_row_for_symbol(symbol, df, rsi, None, None,
                                              atr_pct, None, current_price)
    if seq is None:
        return None

    try:
        x = seq.reshape((1, ML_BRAIN_SEQ_LEN, ML_BRAIN_FEATURES))
        prob = float(model(x, training=False).numpy()[0, 0])
    except Exception as e:
        logging.warning(f"ml_brain: inference failed ({e}); returning no adjustment.")
        return None

    adjustment = (prob - 0.5) * 2 * ML_MAX_SCORE_ADJUSTMENT
    return round(adjustment, 3)


def get_ml_status():
    """Human-readable status for logging/diagnostics -- never raises."""
    try:
        if os.path.exists(ML_META_PATH):
            with open(ML_META_PATH) as f:
                meta = _ml_json.load(f)
            return (f"trained_from={meta.get('trained_from', 'unknown')}, "
                   f"trained_at={meta.get('trained_at')}, "
                   f"n_examples={meta.get('n_examples')}, "
                   f"val_accuracy={meta.get('val_accuracy')}")
        return "no saved model yet"
    except Exception as e:
        return f"status unavailable ({e})"


# Backward-compat shim: this name was used by the previous ML section, kept
# so the main-loop retrain call site doesn't need editing. It now just wraps
# maybe_run_scheduled_ml_training, since the "retrain on every N cycles"
# concept from the old design is replaced by the 17:00 schedule per instruction.
def train_ml_brain_from_live_trades(sess, tf_model, force=False):
    return "live-trade fine-tuning replaced by daily 17:00 ET schedule; see maybe_run_scheduled_ml_training"

# ANSI color codes for terminal output
GREEN = "\033[92m"
RED = "\033[91m"
RESET = "\033[0m"

APIKEYID = os.getenv('APCA_API_KEY_ID')
APISECRETKEY = os.getenv('APCA_API_SECRET_KEY')
APIBASEURL = os.getenv('APCA_API_BASE_URL')

api = tradeapi.REST(APIKEYID, APISECRETKEY, APIBASEURL)

global symbols_to_buy
global price_history, last_stored, interval_map

# ---------------- Configuration flags ----------------
PRINT_SYMBOLS_TO_BUY = False
PRINT_ROBOT_STORED_BUY_AND_SELL_LIST_DATABASE = True
PRINT_DATABASE = True
DEBUG = False
ALL_BUY_ORDERS_ARE_1_DOLLAR = False
FRACTIONAL_BUY_ORDERS = True

# ---------------- 2026 Margin Account Rules ----------------
# The legacy FINRA Pattern Day Trader rule (4 round-trips / 5 business days,
# $25k minimum equity) is no longer enforced by this robot.
# Instead we operate under margin-account risk controls:
ACCOUNT_MODE = 'margin'          # 'margin' or 'cash'
UNLIMITED_DAY_TRADES = True      # No PDT round-trip counting
MAX_PORTFOLIO_EXPOSURE_PCT = 0.98    # of equity (buying power aware)
MAX_LEVERAGE = 1.0               # 1.0 = no borrowing. Raise to 2.0 for Reg-T intraday.
RISK_PER_TRADE_PCT = 0.01        # 1% of equity risked per position
MAX_ALLOCATION_PER_SYMBOL = 600.0
MAX_NEW_POSITIONS_PER_CYCLE = 3  # rank all qualifying candidates, only buy the top N (review item #9)

# ---------------- ML brain (optional buy-score adjustment) ----------------
# Adds a small +/- adjustment to the buy score from the inlined ML brain
# section above, ONLY once it has enough of the bot's own closed-trade
# history to be minimally trustworthy for LIVE decisions (see
# ML_MIN_TRADES_FOR_LIVE_ADJUSTMENT). Below that threshold, or if TensorFlow
# isn't available at all, this contributes NOTHING -- the bot behaves
# exactly as it did before this feature existed.
USE_ML_BRAIN_ADJUSTMENT = True
ML_BRAIN_RETRAIN_EVERY_N_CYCLES = 120   # live-trade fine-tune cadence -- ~2 hours at 60s/cycle, cheap-gated internally too
MIN_ORDER_NOTIONAL = 1.00
CASH_BUFFER = 1.00
MAINTENANCE_MARGIN_FLOOR_PCT = 0.30  # abort new buys if equity/market_value dips below

# ---------------- Hard stop-loss (review items #6/#7) ----------------
# The profit monitor and scaled exits above only ever act once a position is
# ARMED (i.e. already showing a gain) -- there was previously no equivalent
# floor on the downside: a position that went straight from entry to -8%
# with no bounce through the arm threshold had nothing forcing an exit. This
# is a genuine hard stop: checked every sell_stocks() cycle regardless of the
# profit monitor's armed/unarmed state, and fires independently of it.
#
# HARD_STOP_ATR_MULTIPLIER is also the SAME distance position sizing uses to
# compute risk-per-share (see buy_stocks' `risk_per_share = HARD_STOP_ATR_MULTIPLIER * atr`).
# Before this fix, sizing math assumed a 2xATR stop that didn't actually
# exist anywhere in the exit logic -- the "1% risk" the sizing model promised
# was fictional. Now both use the same constant, so a position sized to risk
# RISK_PER_TRADE_PCT of equity is actually capped at that loss by a real stop.
USE_HARD_STOP_LOSS = True
HARD_STOP_ATR_MULTIPLIER = 2.0   # stop at entry_price - (this x current ATR)
HARD_STOP_MIN_PCT = 0.03         # never let a low-ATR stock's stop sit tighter than -3%
                                  # (a near-zero ATR would otherwise produce a razor-thin stop)

# ---------------- Exit strategy ----------------
# Two exits can act on the same shares. A GTC trailing stop RESERVES shares at
# the broker, so a later take-profit sell can only touch the unreserved fraction
# unless the stop is cancelled first (see cancel_open_sell_orders).
#
# IMPORTANT: the trailing stop and the profit monitor are redundant and the stop
# is COARSER. A 1% trailing stop fires long before the monitor's 0.2% giveback
# ever triggers, so leaving both on means the broker-side stop wins every race
# and the peak-following logic never actually runs. USE_TRAILING_STOP therefore
# defaults to False when the monitor is enabled.
USE_TRAILING_STOP = False        # broker-side 1% trailing stop (coarse)
TRAIL_PERCENT = 1.0
TAKE_PROFIT_PCT = 1.005          # +0.5% flat target, only used if monitor is off

# ---------------- Profit Monitor (peak-following exit) ----------------
# There is NO holding-period restriction: a position can be sold the same second
# it is bought. PDT is retired, so same-day round trips are unrestricted.
#
# Rather than dumping at the first tick over +0.5%, the monitor ARMS at that
# level and then follows price up, tracking a high-water mark. It sells when
# price pulls back from the peak by GIVEBACK_PCT, so a run to +3% is captured
# instead of being cut at +0.5%.
USE_PROFIT_MONITOR = True
ARM_PROFIT_PCT = 0.005           # +0.5% flat fallback -> monitor arms and begins following
PEAK_GIVEBACK_PCT = 0.002        # 0.2% flat fallback -> sell after a pullback from the peak
HARD_FLOOR_PCT = 0.001           # never sell armed positions below +0.1% net
MONITOR_STALE_SECS = 900         # drop peak state unseen for 15m (position gone)

# ---------------- Volatility-based profit targets (review item #2) ----------------
# Flat percentages above are used ONLY as a fallback when ATR is unavailable.
# When ATR is available, arm/giveback scale with the stock's own volatility so
# winners can breathe on volatile days while quiet-market trades still bank
# quick, small gains. See ProfitMonitorEngine._arm_threshold_for() and
# ._giveback_for_peak().
ATR_ARM_MULTIPLIER = 0.30        # arm at max(ARM_PROFIT_PCT, 0.30 x ATR%)
ATR_GIVEBACK_FRACTION = 0.20     # giveback floor = 20% of the ARM threshold (small moves)

# REVIEW ITEM #9: the giveback above was originally scaled only off the arm
# threshold, which is fixed once a position arms -- so a stock that ran to
# +1.2% and one that ran to +6% got the SAME tiny giveback room, sacrificing
# a lot of a strong trend's potential move. PEAK_GIVEBACK_FRACTION instead
# scales giveback off the position's ACTUAL peak gain as it grows, so a
# bigger run earns proportionally more room before the monitor sells. The
# arm-based ATR_GIVEBACK_FRACTION above still sets a FLOOR so a position that
# just barely armed doesn't get an unreasonably wide giveback either -- see
# ProfitMonitorEngine._thresholds_for_peak(), which takes max(arm-based floor,
# peak-based scaling).
PEAK_GIVEBACK_FRACTION = 0.20    # giveback = 20% of the peak gain achieved so far

# ---------------- Scaled exits (review item #5) ----------------
# Instead of an all-or-nothing sell, take partial profit in tranches and move
# the effective floor up as each tranche fires, letting the remainder run.
USE_SCALED_EXITS = True
SCALE_OUT_STAGES = [
    # (trigger_gain_pct, fraction_of_ORIGINAL_qty_to_sell)
    (0.010, 0.25),   # +1.0% -> sell 25% of the original position
    (0.020, 0.25),   # +2.0% -> sell another 25% (50% cumulative)
]
# After the LAST configured stage fires, the remaining shares are handed to
# the normal peak-following ProfitMonitorEngine with its stop effectively
# moved to breakeven (HARD_FLOOR_PCT floor already does most of this; the
# scale-out logic additionally refuses to let the remainder exit below
# breakeven once a scale-out stage has fired).

# ---------------- Pre-market open / pre-close profit sweeps ----------------
# At MOO_SWEEP_HOUR:MOO_SWEEP_MINUTE Eastern (default 9:25am) and again at
# CLOSE_SWEEP_HOUR:CLOSE_SWEEP_MINUTE Eastern (default 3:45pm, 15 min before
# the close), check every open position's unrealized P/L and sell any
# position currently showing a profit. Both sweeps use a 3-step escalation
# chain (see _sell_with_escalation) so a sale is not left to chance on a
# single order type: initial order (MOO pre-market / plain market pre-close)
# -> aggressive limit at the bid if that doesn't fill -> plain market order as
# a final guarantee-of-fill fallback. Neither sweep touches or replaces the
# existing intraday profit-monitor / scaled-exit logic, which still runs
# during the regular session as before.
USE_PREMARKET_PROFIT_SWEEP = True
MOO_SWEEP_HOUR = 9
MOO_SWEEP_MINUTE = 25
# Alpaca rejects OPG (market-on-open) orders submitted after approximately
# 9:28am ET. 150 seconds (9:25:00-9:27:30) leaves real margin before that
# cutoff -- covering broker/network latency plus a wait-loop tick that runs
# a bit slow -- rather than running the window right up against 9:29, which
# risks a rejected OPG order on a delayed cycle.
MOO_SWEEP_CUTOFF_SECS = 150
MOO_SWEEP_MIN_PROFIT_PCT = 0.0  # "any profit" -- must be strictly above this

USE_CLOSE_PROFIT_SWEEP = True
CLOSE_SWEEP_HOUR = 15
CLOSE_SWEEP_MINUTE = 45   # 3:45pm ET -- 15 minutes before the 4:00pm close

# ---------------- Portfolio-level liquidation (phase 2 of each sweep) ----------------
# After phase 1 sells whatever individual positions are already profitable,
# check what's LEFT: if the combined unrealized P/L of the remaining
# positions, divided by their combined cost basis, is >= this threshold, sell
# everything that's left -- winners and losers together -- because the
# winners are covering the losers for a net portfolio profit. If the
# remainder doesn't clear the bar, those positions are left alone (this does
# NOT force-sell losers on its own; phase 1's per-position winners still sell
# regardless of what phase 2 decides).
USE_PORTFOLIO_LIQUIDATION_SWEEP = True
PORTFOLIO_LIQUIDATION_MIN_PROFIT_PCT = 0.01   # 1% combined; raise to 0.02 for 2%

# ---------------- Threading ----------------
# Worker threads are joined with a timeout so a hung API call in one thread
# cannot freeze the main loop indefinitely.
THREAD_JOIN_TIMEOUT = 180

eastern = pytz.timezone('US/Eastern')

stock_data = {}
previous_prices = {}
price_changes = {}

price_history = {}
last_stored = {}
interval_map = {
    '1min': 60, '5min': 300, '10min': 600, '15min': 900,
    '30min': 1800, '45min': 2700, '60min': 3600
}

buy_sell_lock = threading.Lock()
yf_lock = threading.Lock()

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(filename=os.path.join(_BASE_DIR, 'trading-bot-program-logging-messages.txt'),
                    level=logging.INFO)

# BUGFIX: relative path meant the trade log followed the launch directory, same
# as the .db issue. Anchor it to the script directory.
csv_filename = os.path.join(_BASE_DIR, 'log-file-of-buy-and-sell-signals.csv')
fieldnames = ['Date', 'Buy', 'Sell', 'Quantity', 'Symbol', 'Price Per Share']

if not os.path.exists(csv_filename):
    with open(csv_filename, mode='w', newline='') as csv_file:
        csv.DictWriter(csv_file, fieldnames=fieldnames).writeheader()

Base = sqlalchemy.orm.declarative_base()


class TradeHistory(Base):
    __tablename__ = 'trade_history'
    id = Column(Integer, primary_key=True)
    symbols = Column(String)
    action = Column(String)
    quantity = Column(Float)
    price = Column(Float)
    date = Column(String)


class Position(Base):
    __tablename__ = 'positions'
    symbols = Column(String, primary_key=True)
    quantity = Column(Float)
    avg_price = Column(Float)
    purchase_date = Column(String)


class TradeFeatures(Base):
    """
    REVIEW ITEM #7: record the features present at entry for every buy, plus
    the eventual outcome (filled in when the position closes), so trade
    history can be analyzed to see which feature combinations actually
    produced profitable trades rather than relying only on fixed assumptions.
    """
    __tablename__ = 'trade_features'
    id = Column(Integer, primary_key=True)
    symbols = Column(String)
    entry_date = Column(String)
    entry_price = Column(Float)
    rsi = Column(Float)
    macd_above_signal = Column(Integer)      # 0/1
    atr_pct = Column(Float)
    volume_holding = Column(Integer)         # 0/1
    candlestick_pattern = Column(String)
    buy_score = Column(Float)
    regime = Column(String)
    time_of_day = Column(String)
    exit_date = Column(String)               # filled on close
    exit_price = Column(Float)               # filled on close
    outcome_pct = Column(Float)              # filled on close: (exit-entry)/entry


class AdaptiveParamState(Base):
    """
    Persisted current value of one auto-adjusted parameter, so a restart
    resumes from the last-learned state instead of resetting to the coded
    default. One row per (param_name, regime).
    """
    __tablename__ = 'adaptive_param_state'
    id = Column(Integer, primary_key=True)
    param_name = Column(String)   # e.g. 'buy_score_threshold'
    regime = Column(String)       # e.g. 'bull' / 'bear' / ... or 'global'
    value = Column(Float)
    updated_at = Column(String)


class AdaptiveParamLog(Base):
    """
    Audit trail: every automatic adjustment the bot makes to its own
    parameters, with the reasoning, so behavior changes are never silent.
    """
    __tablename__ = 'adaptive_param_log'
    id = Column(Integer, primary_key=True)
    timestamp = Column(String)
    param_name = Column(String)
    regime = Column(String)
    old_value = Column(Float)
    new_value = Column(Float)
    sample_size = Column(Integer)
    reason = Column(String)


# ---------------- Database ----------------
# BUGFIX: the path was relative ('sqlite:///trading_bot.db'), so the DB was
# created in whatever directory the program was launched from. Starting the bot
# from a different cwd silently opened a DIFFERENT, EMPTY database -- which
# looks exactly like "the .db stopped working after a restart". Anchor it to the
# script's own directory so it is always the same file.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trading_bot.db')
print(f"Using database: {DB_PATH}")

engine = create_engine(
    f'sqlite:///{DB_PATH}',
    connect_args={
        'check_same_thread': False,
        # BUGFIX: with two writer threads, lock contention raised
        # "database is locked" and the write was lost. Wait instead of failing.
        'timeout': 30,
    },
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _record):
    """
    BUGFIX: default journal mode gives poor durability and concurrency for a
    two-thread writer. WAL allows a reader alongside a writer and survives an
    ungraceful kill (e.g. Ctrl-C mid-write) without corrupting the file.
    """
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")      # crash-safe, concurrent reads
    cur.execute("PRAGMA synchronous=FULL")      # fsync on commit; survives power loss
    cur.execute("PRAGMA busy_timeout=30000")    # wait 30s for locks, don't error
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


# BUGFIX: buy_stocks and sell_stocks run as concurrent threads and previously
# shared one module-level Session. SQLAlchemy Sessions are NOT thread-safe, and
# both threads call session.query() outside any lock, so this could corrupt the
# identity map or raise intermittent InvalidRequestError once positions existed.
# scoped_session hands each thread its own session behind the same API.
Session = scoped_session(sessionmaker(bind=engine))
session = Session
Base.metadata.create_all(engine)

data_cache = {}
# BUGFIX: data_cache is read AND written by both worker threads with no
# synchronization. Guard it.
_cache_lock = threading.Lock()
CACHE_EXPIRY = 120                 # default: intraday prices

# Tiered TTLs. Daily-bar data (200-day SMA, daily RSI, 22-period ATR) barely
# moves during a session, but was being refetched every cycle -- the single
# biggest consumer of the yfinance budget. 16 symbols x 5 requests = 80/cycle
# against a 55/min cap would throttle every cold pass. Caching daily series for
# 30 minutes keeps steady-state well under the cap.
CACHE_TTLS = {
    'current_price': 120,          # 2m  - needs to be fresh
    'atr': 1800,                   # 30m - 22-period daily ATR
    'uptrend': 1800,               # 30m - 200-day SMA
    'daily_rsi': 1800,             # 30m - 14-period daily RSI
    'history_90d': 900,            # 15m - daily candles for scoring
    'regime': 900,                 # 15m - VIX + SPY market regime classification
    'mtf_60m': 900,                # 15m - 60-minute intraday trend confirmation
    'mtf_5m': 300,                 # 5m  - 5-minute reversal confirmation
    'earnings_date': 21600,        # 6h  - next earnings date per symbol
    'relative_strength': 1800,     # 30m - RS vs SPY / sector proxy
}

CALLS = 60
PERIOD = 60

# ---------------- yfinance rate limiting ----------------
# yfinance guidance: 60 req/min (1/sec) is very safe, 120/min (2/sec) usually
# safe, plus a 0.5-1s delay BETWEEN requests. Batch where practical.
#
# BUGFIX: @limits creates a SEPARATE counter per decorated function -- they do
# NOT share a budget. Five different functions call yfinance
# (_fetch_current_price, _fetch_atr, is_in_uptrend, get_daily_rsi,
# calculate_technical_indicators), each decorated @limits(calls=60, period=60),
# which permitted 5 x 60 = 300 yfinance calls/minute. buy_stocks also called
# yf.Ticker(...).history() directly with NO limit at all.
#
# Every yfinance request now passes through ONE shared gate, so the cap is a real
# 60/min across the whole process regardless of which thread or function calls it.
YF_CALLS_PER_MIN = 55        # under the 60/min "very safe" guidance
YF_MIN_INTERVAL = 0.6        # seconds between requests, per yfinance guidance


class _YFGate:
    """
    Process-wide gate for yfinance. Thread-safe; shared by both workers.

    Enforces BOTH:
      - a rolling 60s ceiling (YF_CALLS_PER_MIN), and
      - a minimum spacing between consecutive requests (YF_MIN_INTERVAL).

    BUGFIX: the rolling window ALONE let all 55 requests fire in the same
    millisecond and then stall 60s (measured: 55 requests in 0.000s). The average
    is technically legal but that burst is exactly what triggers throttling --
    yfinance guidance asks for a 0.5-1s delay BETWEEN requests. Now enforced.
    """

    def __init__(self, calls_per_min, min_interval=0.0):
        self.capacity = calls_per_min
        self.min_interval = min_interval
        self._times = deque()
        self._last_call = 0.0
        self._lock = threading.Lock()

    def acquire(self):
        """Block until it is polite to issue the next yfinance request."""
        while True:
            with self._lock:
                now = time.monotonic()
                while self._times and now - self._times[0] >= 60.0:
                    self._times.popleft()

                since_last = now - self._last_call
                if self.min_interval and since_last < self.min_interval:
                    wait = self.min_interval - since_last          # spacing
                elif len(self._times) >= self.capacity:
                    wait = 60.0 - (now - self._times[0]) + 0.01    # window cap
                    logging.info(f"yfinance gate: at {self.capacity}/min cap, waiting {wait:.1f}s")
                else:
                    self._times.append(now)
                    self._last_call = now
                    return
            # Sleep OUTSIDE the lock so other threads can drain expired slots.
            time.sleep(min(wait, 5.0))

    def used(self):
        with self._lock:
            now = time.monotonic()
            while self._times and now - self._times[0] >= 60.0:
                self._times.popleft()
            return len(self._times)


yf_gate = _YFGate(YF_CALLS_PER_MIN, YF_MIN_INTERVAL)


def yf_history(symbol, **kwargs):
    """
    The ONLY way this program should make a single-symbol yfinance call. Routing
    every request through one function makes the shared cap enforceable.
    Prefer yf_download_batch() when fetching the same period for many symbols.
    """
    yf_gate.acquire()
    with yf_lock:
        return yf.Ticker(to_yf(symbol)).history(**kwargs)


def yf_download_batch(symbols, **kwargs):
    """
    Batched multi-symbol fetch. yfinance guidance: prefer ONE
    yf.download(["AAPL","MSFT",...]) over N per-ticker requests, since batching
    dramatically reduces throttling risk.

    Returns {symbol: DataFrame}. Symbols that came back empty are omitted.
    Consumes ONE slot from the gate regardless of symbol count.
    """
    syms = [to_yf(s) for s in symbols]
    if not syms:
        return {}

    yf_gate.acquire()
    with yf_lock:
        raw = yf.download(syms, group_by='ticker', progress=False,
                          auto_adjust=False, threads=False, **kwargs)

    out = {}
    for orig, ys in zip(symbols, syms):
        try:
            # Multi-symbol returns column MultiIndex; single symbol returns flat.
            df = raw[ys] if len(syms) > 1 else raw
            if df is not None and not df.empty and not df['Close'].isna().all():
                out[orig] = df.dropna(how='all')
        except (KeyError, TypeError):
            continue
    return out


def prewarm_daily_cache(symbols):
    """
    Fetch a year of daily bars for every symbol in ONE batched request and seed
    the cache with the derived 200-day SMA, daily RSI and ATR.

    PERF: previously each symbol cost 3 separate yfinance requests for these
    (is_in_uptrend 1y + get_daily_rsi 60d + _fetch_atr 60d). For 16 symbols that
    was 48 requests; batched it is 1. All three are daily series, so one 1y pull
    serves all of them.
    """
    if not symbols:
        return

    # Skip entirely if every symbol still has warm daily entries -- otherwise the
    # batched call itself would waste a gate slot every cycle.
    now = time.time()
    ttl = CACHE_TTLS.get('uptrend', 1800)
    with _cache_lock:
        stale = [s for s in symbols
                 if now - data_cache.get((s, 'uptrend'), {}).get('timestamp', 0) >= ttl]
    if not stale:
        return

    try:
        batch = yf_download_batch(stale, period='1y', interval='1d')
    except Exception as e:
        logging.warning(f"Batched daily prewarm failed ({e}); falling back to per-symbol fetches.")
        return

    seeded = 0
    for sym, df in batch.items():
        try:
            close = df['Close'].values
            entries = {}

            if len(close) >= 200:
                sma = talib.SMA(close, timeperiod=200)[-1]
                if np.isfinite(sma):
                    entries['uptrend'] = float(sma)

            if len(close) >= 15:
                r = talib.RSI(close, timeperiod=14)[-1]
                if np.isfinite(r):
                    entries['daily_rsi'] = round(float(r), 2)

            if len(df) >= 23:
                atr = talib.ATR(df['High'].values, df['Low'].values, close, timeperiod=22)[-1]
                if np.isfinite(atr) and atr > 0:
                    entries['atr'] = float(atr)

            if len(df) >= 40:
                entries['history_90d'] = df.tail(90)

            with _cache_lock:
                for k, v in entries.items():
                    data_cache[(sym, k)] = {'timestamp': now, 'data': v}
            if entries:
                seeded += 1
        except Exception as e:
            logging.warning(f"Prewarm: could not derive indicators for {sym}: {e}")

    print(f"Prewarmed daily cache for {seeded}/{len(stale)} symbols in 1 batched request "
          f"(~{len(stale) * 3} individual requests avoided).")


# ---------------- Symbol helpers (BUGFIX: consistent normalization) ----------------
def to_yf(sym):
    """yfinance uses dashes for share classes: BRK.B -> BRK-B"""
    return sym.strip().upper().replace('.', '-')


def to_alpaca(sym):
    """Alpaca uses dots: BRK-B -> BRK.B"""
    return sym.strip().upper().replace('-', '.')


# BUGFIX: get_cached_data used to be @sleep_and_retry/@limits decorated. That was
# wrong twice over:
#   1. A CACHE HIT -- which makes no network call at all -- still consumed a slot
#      from the shared 60/min budget.
#   2. It NESTED: get_current_price -> get_cached_data -> _fetch_current_price,
#      all three rate-limited, so ONE price lookup burned THREE slots. With ~16
#      symbols the budget was exhausted mid-cycle, and sleep_and_retry then SLEEPS
#      THE CALLING THREAD for up to a full 60s window -- while sell_stocks held a
#      per-symbol claim, locking buy_stocks out of that symbol the entire time.
#      (Verified: 70 calls against the real limiter block for exactly 60.0s.)
# The cache layer does no I/O, so it is no longer rate-limited. Only the real
# network fetchers below are.
def get_cached_data(symbols, data_type, fetch_func, *args, **kwargs):
    key = (symbols, data_type)
    ttl = CACHE_TTLS.get(data_type, CACHE_EXPIRY)
    now = time.time()

    with _cache_lock:
        entry = data_cache.get(key)
        if entry and now - entry['timestamp'] < ttl:
            return entry['data']

    # Fetch OUTSIDE the cache lock: fetch_func is rate-limited and can block for
    # a full window. Holding _cache_lock across it would stall every cache reader
    # in both threads.
    data = fetch_func(*args, **kwargs)

    with _cache_lock:
        data_cache[key] = {'timestamp': time.time(), 'data': data}
    return data


# ---------------- Market regime detection ----------------
# Classifies the overall market as Bull / Sideways / Bear / Panic using VIX
# level plus SPY's position relative to its 20/50-day SMAs. Buy-score signal
# weights and the buy threshold both key off this classification, per the
# review's recommendation to stop treating every signal as equally important
# in every market.
REGIME_BULL = 'bull'
REGIME_SIDEWAYS = 'sideways'
REGIME_BEAR = 'bear'
REGIME_PANIC = 'panic'

# VIX thresholds (approximate historical bands)
VIX_PANIC_LEVEL = 30.0
VIX_ELEVATED_LEVEL = 20.0

# Per-regime signal weights. Keys must match the boolean/points computed in
# compute_buy_score. Unlisted keys default to weight 1.
REGIME_SIGNAL_WEIGHTS = {
    REGIME_BULL: {
        'pattern': 1, 'rsi_below_50': 1, 'rsi_falling': 1, 'volume_holding': 1,
        'macd_above_signal': 1, 'price_decline': 1, 'pattern_bonus': 1,
        'price_stable': 1, 'trend': 3, 'intraday_pullback': 1,
    },
    REGIME_SIDEWAYS: {
        'pattern': 2, 'rsi_below_50': 1, 'rsi_falling': 1, 'volume_holding': 1,
        'macd_above_signal': 1, 'price_decline': 1, 'pattern_bonus': 1,
        'price_stable': 1, 'trend': 1, 'intraday_pullback': 1,
    },
    REGIME_BEAR: {
        'pattern': 2, 'rsi_below_50': 3, 'rsi_falling': 1, 'volume_holding': 2,
        'macd_above_signal': 3, 'price_decline': 1, 'pattern_bonus': 1,
        'price_stable': 1, 'trend': 1, 'intraday_pullback': 2,
    },
    REGIME_PANIC: {
        'pattern': 1, 'rsi_below_50': 3, 'rsi_falling': 1, 'volume_holding': 2,
        'macd_above_signal': 3, 'price_decline': 1, 'pattern_bonus': 1,
        'price_stable': 1, 'trend': 1, 'intraday_pullback': 2,
    },
}

# Dynamic buy-score threshold per regime (feature #6 in the review). Higher
# volatility / more danger -> require stronger confirmation before buying.
# These are the STARTING values only. The live values the bot actually trades
# with are auto-adjusted within bounds by AdaptiveParams (see below) and
# persisted to the database, so a restart resumes from the last-learned state
# instead of snapping back to these defaults.
REGIME_BUY_THRESHOLDS_DEFAULT = {
    REGIME_BULL: 3,
    REGIME_SIDEWAYS: 4,
    REGIME_BEAR: 5,
    REGIME_PANIC: 6,
}
# Hard bounds the auto-adjuster can never move outside of, regardless of what
# the trade history seems to suggest. Keeps a run of lucky/unlucky trades from
# ever turning the bot reckless (threshold too low) or completely inert
# (threshold too high).
REGIME_BUY_THRESHOLD_BOUNDS = {
    REGIME_BULL: (2, 5),
    REGIME_SIDEWAYS: (3, 6),
    REGIME_BEAR: (4, 8),
    REGIME_PANIC: (5, 9),
}


def _fetch_market_regime():
    """
    Classify the market using ^VIX level and SPY's close relative to its
    20-day and 50-day SMA. Cached for 15 minutes (CACHE_TTLS['regime']) since
    regime does not flip minute-to-minute.
    """
    try:
        vix_hist = yf_history('^VIX', period='5d', interval='1d')
        vix_level = float(vix_hist['Close'].iloc[-1]) if not vix_hist.empty else None
    except Exception as e:
        logging.warning(f"Regime: VIX fetch failed: {e}")
        vix_level = None

    try:
        spy_hist = yf_history('SPY', period='90d', interval='1d')
        if spy_hist.empty or len(spy_hist) < 55:
            spy_close = spy_sma20 = spy_sma50 = None
        else:
            close = spy_hist['Close'].values
            spy_close = float(close[-1])
            spy_sma20 = float(talib.SMA(close, timeperiod=20)[-1])
            spy_sma50 = float(talib.SMA(close, timeperiod=50)[-1])
    except Exception as e:
        logging.warning(f"Regime: SPY fetch failed: {e}")
        spy_close = spy_sma20 = spy_sma50 = None

    # Panic overrides everything: extreme VIX means bear-style caution
    # regardless of where SPY sits relative to its averages.
    if vix_level is not None and vix_level >= VIX_PANIC_LEVEL:
        regime = REGIME_PANIC
    elif spy_close is not None and spy_sma20 is not None and spy_sma50 is not None:
        if spy_close > spy_sma20 > spy_sma50:
            regime = REGIME_BULL
        elif spy_close < spy_sma20 < spy_sma50:
            regime = REGIME_BEAR
        else:
            regime = REGIME_SIDEWAYS
        # Elevated VIX in a non-bull SPY posture still tightens things up.
        if vix_level is not None and vix_level >= VIX_ELEVATED_LEVEL and regime != REGIME_BULL:
            regime = REGIME_BEAR
    else:
        # Missing data: default to the conservative middle ground rather than
        # silently trading as if conditions were calm.
        regime = REGIME_SIDEWAYS

    return {
        'regime': regime, 'vix': vix_level,
        'spy_close': spy_close, 'spy_sma20': spy_sma20, 'spy_sma50': spy_sma50,
    }


# ---------------- Auto-adapting parameters with safety guardrails ----------------
# Point-based auto-adjuster for BUY_SCORE_THRESHOLD-per-regime and the
# per-regime signal weights. Every ADAPT_EVERY_N_CYCLES, this scores each
# regime's recent closed trades and nudges parameters toward what's working --
# automatically, no human approval step -- but ONLY within hard guardrails:
#
#   1. MIN SAMPLE SIZE: a regime with too few closed trades is left alone.
#      Small samples are how you get a threshold that "learned" from 3 lucky
#      trades. No sample, no move.
#   2. MAX STEP SIZE: every adjustment moves a parameter by at most one small
#      step per cycle. The bot drifts toward better settings over many
#      windows; it can never jump to an extreme in one adjustment.
#   3. HARD BOUNDS: REGIME_BUY_THRESHOLD_BOUNDS / weight bounds below are
#      ceilings and floors the adjuster can never cross, no matter how
#      strongly the data seems to argue for it.
#   4. FULL AUDIT LOG: every adjustment (or decision NOT to adjust) is written
#      to AdaptiveParamLog with the sample size and reasoning, and printed to
#      the console. Nothing changes silently.
#   5. PERSISTED, NOT RESET: current values live in AdaptiveParamState, so a
#      restart resumes from the last-learned value instead of the coded
#      default -- but also means a bad drift persists until corrected, which
#      is exactly why 1-4 exist.
ADAPT_EVERY_N_CYCLES = 60             # ~1 hour at 60s/cycle
ADAPT_MIN_TRADES_PER_REGIME = 15      # guardrail #1: floor on sample size
ADAPT_MIN_WIN_RATE_SAMPLE = 8         # per-bucket floor when scoring weight point-deltas
THRESHOLD_STEP = 1                    # guardrail #2: max threshold move per cycle
WEIGHT_STEP = 1                       # guardrail #2: max per-signal weight move per cycle
WEIGHT_BOUNDS = (1, 4)                # guardrail #3 for signal weights (pattern, rsi, macd, etc.)
# A regime needs a clearly better/worse out-of-sample result -- not noise --
# before the threshold moves at all. Expressed as a minimum gap in average
# outcome_pct between "trades that would have passed a stricter/looser bar".
ADAPT_MIN_EDGE_PCT = 0.0015           # 0.15 percentage points of average return


class AdaptiveParams:
    """
    Thread-safe, DB-persisted store for the live values of auto-adjusted
    parameters. Reads are cheap (in-memory dict behind a lock); writes go
    through `_persist` and are logged via AdaptiveParamLog.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._thresholds = dict(REGIME_BUY_THRESHOLDS_DEFAULT)
        self._weights = {r: dict(w) for r, w in REGIME_SIGNAL_WEIGHTS.items()}
        self._loaded = False

    # ---- loading / persistence ----
    def load_from_db(self):
        """Resume from the last-learned state on startup, if any exists."""
        with self._lock:
            if self._loaded:
                return
            rows = session.query(AdaptiveParamState).all()
            for row in rows:
                if row.param_name == 'buy_score_threshold' and row.regime in self._thresholds:
                    self._thresholds[row.regime] = row.value
                elif row.param_name.startswith('weight:'):
                    signal_key = row.param_name.split('weight:', 1)[1]
                    if row.regime in self._weights:
                        self._weights[row.regime][signal_key] = row.value
            self._loaded = True
            if rows:
                print(f"AdaptiveParams: resumed {len(rows)} persisted parameter values from prior runs.")

    def _persist(self, param_name, regime, value):
        row = (session.query(AdaptiveParamState)
               .filter_by(param_name=param_name, regime=regime).one_or_none())
        now_str = datetime.now(eastern).strftime("%Y-%m-%d %H:%M:%S")
        if row is None:
            row = AdaptiveParamState(param_name=param_name, regime=regime,
                                     value=value, updated_at=now_str)
            session.add(row)
        else:
            row.value = value
            row.updated_at = now_str

    def _log(self, param_name, regime, old_value, new_value, sample_size, reason):
        now_str = datetime.now(eastern).strftime("%Y-%m-%d %H:%M:%S")
        session.add(AdaptiveParamLog(
            timestamp=now_str, param_name=param_name, regime=regime,
            old_value=old_value, new_value=new_value,
            sample_size=sample_size, reason=reason,
        ))
        arrow = '->' if old_value != new_value else '(unchanged)'
        print(f"  [adapt] {param_name} [{regime}]: {old_value} {arrow} {new_value}  "
              f"(n={sample_size}) {reason}")

    # ---- reads (used by live trading code every cycle) ----
    def get_threshold(self, regime):
        with self._lock:
            return self._thresholds.get(regime, BUY_SCORE_THRESHOLD_DEFAULT)

    def get_weights(self, regime):
        with self._lock:
            return dict(self._weights.get(regime, self._weights[REGIME_SIDEWAYS]))

    # ---- the adjustment pass itself ----
    def run_adjustment_pass(self):
        """
        Called periodically from the main loop. For each regime with enough
        closed trades (guardrail #1), compares outcomes above vs. below the
        current threshold and nudges the threshold by at most THRESHOLD_STEP
        (guardrail #2) toward whichever side has the better average outcome,
        clamped to REGIME_BUY_THRESHOLD_BOUNDS (guardrail #3). Every decision
        -- move or no-move -- is logged (guardrail #4).
        """
        all_rows = (session.query(TradeFeatures)
                    .filter(TradeFeatures.outcome_pct.isnot(None))
                    .all())
        by_regime = {}
        for r in all_rows:
            by_regime.setdefault(r.regime or REGIME_SIDEWAYS, []).append(r)

        print("\n--- Adaptive Parameter Pass (auto-applies within guardrails) ---")
        with self._lock:
            for regime in (REGIME_BULL, REGIME_SIDEWAYS, REGIME_BEAR, REGIME_PANIC):
                rows = by_regime.get(regime, [])
                self._adjust_threshold_for_regime(regime, rows)
                self._adjust_weights_for_regime(regime, rows)
        session.commit()
        print("--- End adaptive parameter pass ---\n")

    def _adjust_threshold_for_regime(self, regime, rows):
        n = len(rows)
        lo, hi = REGIME_BUY_THRESHOLD_BOUNDS[regime]
        current = self._thresholds[regime]

        if n < ADAPT_MIN_TRADES_PER_REGIME:
            self._log('buy_score_threshold', regime, current, current, n,
                      f"below min sample ({ADAPT_MIN_TRADES_PER_REGIME}); holding steady.")
            return

        # Point-based comparison: trades that scored AT the current threshold
        # vs. trades that scored one point ABOVE it. If the higher bar clearly
        # outperforms, tighten (raise threshold). If trades right at the
        # current bar do just as well or better than stricter ones, and the
        # bar is already above its floor, loosen it one point to trade more.
        at_bar = [r.outcome_pct for r in rows if r.buy_score is not None
                  and current <= r.buy_score < current + 1]
        above_bar = [r.outcome_pct for r in rows if r.buy_score is not None
                    and r.buy_score >= current + 1]

        new_value = current
        reason = "no clear edge either direction; holding steady."

        if len(above_bar) >= ADAPT_MIN_WIN_RATE_SAMPLE and len(at_bar) >= ADAPT_MIN_WIN_RATE_SAMPLE:
            edge = float(np.mean(above_bar)) - float(np.mean(at_bar))
            if edge >= ADAPT_MIN_EDGE_PCT and current + THRESHOLD_STEP <= hi:
                new_value = current + THRESHOLD_STEP
                reason = (f"scores >= {current+1} outperformed scores at {current} by "
                          f"{edge*100:+.2f}pp; tightening.")
            elif edge <= -ADAPT_MIN_EDGE_PCT and current - THRESHOLD_STEP >= lo:
                new_value = current - THRESHOLD_STEP
                reason = (f"scores at {current} outperformed scores >= {current+1} by "
                          f"{-edge*100:+.2f}pp; loosening to trade more of what's working.")
            else:
                reason = f"edge {edge*100:+.2f}pp within noise band (±{ADAPT_MIN_EDGE_PCT*100:.2f}pp); holding."
        else:
            reason = (f"not enough trades on both sides of the bar "
                      f"(at={len(at_bar)}, above={len(above_bar)}, need {ADAPT_MIN_WIN_RATE_SAMPLE}+ each); holding.")

        if new_value != current:
            self._thresholds[regime] = new_value
            self._persist('buy_score_threshold', regime, new_value)
        self._log('buy_score_threshold', regime, current, new_value, n, reason)

    def _adjust_weights_for_regime(self, regime, rows):
        """
        Point-based weight adjustment: for each signal key present in this
        regime's weight table, compare the average outcome of trades where
        that signal fired vs. trades where it didn't. A clearly-helpful
        signal's weight nudges up by WEIGHT_STEP; a clearly-unhelpful one
        nudges down. Both clamped to WEIGHT_BOUNDS. Every signal needs its
        own minimum sample on both sides (fired/not-fired) before it moves.
        """
        n = len(rows)
        if n < ADAPT_MIN_TRADES_PER_REGIME:
            return  # already logged by the threshold check above for this regime

        weights = self._weights[regime]
        # Only these signals are inferable from what's stored per-trade today
        # (TradeFeatures doesn't currently break out every raw sub-signal --
        # macd and pattern presence are the ones we can score point-by-point).
        signal_checks = {
            'macd_above_signal': lambda r: bool(r.macd_above_signal),
            'volume_holding': lambda r: bool(r.volume_holding),
        }
        for signal_key, predicate in signal_checks.items():
            fired = [r.outcome_pct for r in rows if predicate(r)]
            not_fired = [r.outcome_pct for r in rows if not predicate(r)]
            current_w = weights.get(signal_key, 1)
            lo, hi = WEIGHT_BOUNDS

            if len(fired) < ADAPT_MIN_WIN_RATE_SAMPLE or len(not_fired) < ADAPT_MIN_WIN_RATE_SAMPLE:
                continue  # not enough trades on both sides; leave this weight alone

            edge = float(np.mean(fired)) - float(np.mean(not_fired))
            new_w = current_w
            reason = f"edge {edge*100:+.2f}pp within noise band; holding."
            if edge >= ADAPT_MIN_EDGE_PCT and current_w + WEIGHT_STEP <= hi:
                new_w = current_w + WEIGHT_STEP
                reason = f"trades with {signal_key} outperformed by {edge*100:+.2f}pp; raising weight."
            elif edge <= -ADAPT_MIN_EDGE_PCT and current_w - WEIGHT_STEP >= lo:
                new_w = current_w - WEIGHT_STEP
                reason = f"trades with {signal_key} underperformed by {-edge*100:+.2f}pp; lowering weight."

            if new_w != current_w:
                weights[signal_key] = new_w
                self._persist(f'weight:{signal_key}', regime, new_w)
            self._log(f'weight:{signal_key}', regime, current_w, new_w, n, reason)


adaptive_params = AdaptiveParams()


def get_market_regime():
    return get_cached_data('MARKET', 'regime', _fetch_market_regime)


def get_buy_score_threshold(regime=None):
    regime = regime or get_market_regime()['regime']
    return adaptive_params.get_threshold(regime)


def get_regime_weights(regime=None):
    regime = regime or get_market_regime()['regime']
    return adaptive_params.get_weights(regime)


_moo_sweep_lock = threading.Lock()
_moo_sweep_last_run_date = None   # 'YYYY-MM-DD' of the last date the AM sweep ran
_close_sweep_lock = threading.Lock()
_close_sweep_last_run_date = None  # 'YYYY-MM-DD' of the last date the PM sweep ran

# ---------------- Escalation chain config (both sweeps share this) ----------------
# Step 1: MOO (pre-market) or plain market order (pre-close) is submitted.
# Step 2: if it isn't confirmed/filled within ESCALATION_STEP1_TIMEOUT_SECS,
#         cancel it and submit an aggressive limit order at (or near) the
#         current bid/last price.
# Step 3: if THAT isn't filled within ESCALATION_STEP2_TIMEOUT_SECS, cancel it
#         and submit a plain market order, which will fill at whatever the
#         prevailing price is. This step is what makes the sweep a "sell no
#         matter what" rather than a "try to sell" -- the only things that can
#         still stop it are a trading halt or the broker being unreachable.
ESCALATION_STEP1_TIMEOUT_SECS = 90    # time to wait for the MOO/initial order
ESCALATION_STEP2_TIMEOUT_SECS = 60    # time to wait for the fallback limit order
LIMIT_FALLBACK_DISCOUNT_PCT = 0.0     # 0.0 = limit AT current bid/last price, no discount

# Scale-out stage orders are plain market orders during regular session hours
# and should confirm within a few seconds -- a much shorter budget than the
# sweep escalation timeouts above, so sell_stocks() isn't blocked for up to
# 90s per symbol per cycle while iterating many positions.
SCALE_OUT_FILL_TIMEOUT_SECS = 15


def _in_time_window(now, hour, minute, window_minutes=4):
    """True from hour:minute through the next window_minutes Eastern."""
    start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return start <= now < start + timedelta(minutes=window_minutes)


def _in_moo_sweep_window(now):
    """
    True from MOO_SWEEP_HOUR:MOO_SWEEP_MINUTE through MOO_SWEEP_CUTOFF_SECS
    later (default 9:25:00-9:27:30 Eastern). Alpaca rejects OPG (market-on-
    open) orders submitted after approximately 9:28am ET, so this window ends
    well before that cutoff -- with margin for network/broker latency between
    when the bot decides to submit and when Alpaca actually receives it, plus
    tolerance for the wait loop's 60s tick occasionally running slow. The
    once-per-day guard in run_premarket_profit_sweep() still ensures it only
    fires once even though the window spans multiple loop ticks.
    """
    start = now.replace(hour=MOO_SWEEP_HOUR, minute=MOO_SWEEP_MINUTE, second=0, microsecond=0)
    return start <= now < start + timedelta(seconds=MOO_SWEEP_CUTOFF_SECS)


def _in_close_sweep_window(now):
    """True in the window around CLOSE_SWEEP_HOUR:CLOSE_SWEEP_MINUTE Eastern
    (default 3:45pm, i.e. 15 minutes before the 4:00pm close)."""
    return _in_time_window(now, CLOSE_SWEEP_HOUR, CLOSE_SWEEP_MINUTE)


def _get_bid_or_last_price(symbol, fallback_price):
    """Best-effort current bid for the aggressive fallback limit price; falls
    back to the last-known price (from the position or a fresh quote) if a
    live quote isn't available."""
    try:
        quote = api.get_latest_quote(symbol)
        bid = float(getattr(quote, 'bid_price', 0) or 0)
        if bid > 0:
            return bid
    except Exception as e:
        logging.info(f"{symbol}: latest-quote lookup failed ({e}); using fallback price.")
    return fallback_price


def _poll_order_terminal(order_id, timeout_secs, poll_every=2):
    """
    Poll an order until it reaches a terminal state (filled/canceled/expired/
    rejected) or timeout_secs elapses. Returns (terminal: bool, filled_qty,
    filled_price, status). Mirrors the polling pattern already used for buy
    fills in buy_stocks().
    """
    filled_qty, filled_price, status = 0.0, None, 'unknown'
    elapsed = 0
    while elapsed < timeout_secs:
        try:
            o = api.get_order(order_id)
        except Exception as e:
            logging.warning(f"Order {order_id}: poll error ({e}); retrying.")
            time.sleep(poll_every)
            elapsed += poll_every
            continue

        filled_qty = float(o.filled_qty or 0)
        if o.filled_avg_price:
            filled_price = float(o.filled_avg_price)
        status = o.status

        if status == 'filled':
            return True, filled_qty, filled_price, status
        if status in ('canceled', 'expired', 'rejected'):
            return True, filled_qty, filled_price, status

        time.sleep(poll_every)
        elapsed += poll_every

    return False, filled_qty, filled_price, status


def _cancel_existing_sell_orders(symbol, sweep_label, now_str):
    """
    Cancel any pre-existing open SELL orders for `symbol` (e.g. a resting
    stop-loss or GTC limit placed earlier by the bot's normal intraday exit
    logic) so the profit sweeps always take full, unencumbered ownership of
    the position instead of only working around whatever quantity those
    orders already cover. Always cancels -- per instruction, the sweep should
    always control the whole position rather than leave a fraction tied up in
    an older order, regardless of that order's price.

    Returns the number of orders cancelled (0 if none existed or cancel
    failed -- a failure here is logged but does not block the sweep from
    still trying to sell, since list_orders will simply keep counting that
    quantity as "already selling" if the cancel didn't go through).
    """
    try:
        open_orders = api.list_orders(status='open', symbols=[symbol])
    except Exception as e:
        logging.warning(f"{symbol}: [{sweep_label}] list_orders failed while checking for "
                        f"pre-existing sell orders ({e}); proceeding without cancelling.")
        return 0

    sell_orders = [o for o in open_orders if o.side == 'sell']
    cancelled = 0
    for o in sell_orders:
        try:
            api.cancel_order(o.id)
            cancelled += 1
            print(f"{symbol}: [{sweep_label}] cancelled pre-existing open sell order "
                  f"{o.id} ({o.qty} sh) so the sweep can own the full position.")
            logging.info(f"{now_str} [{sweep_label}] {symbol}: cancelled pre-existing sell "
                        f"order {o.id} ({o.qty} sh).")
        except Exception as e:
            print(f"{symbol}: [{sweep_label}] failed to cancel pre-existing sell order "
                  f"{o.id}: {e}. That quantity may remain tied up.")
            logging.warning(f"{now_str} [{sweep_label}] {symbol}: cancel of pre-existing "
                            f"sell order {o.id} failed: {e}")

    if cancelled:
        # Give the broker a beat to process the cancellations before we re-check
        # qty and submit the sweep's own order, so we don't race a cancel that
        # hasn't landed yet.
        time.sleep(1)

    return cancelled


def _sell_with_escalation(symbol, qty, last_known_price, step1_type, step1_tif,
                          sweep_label, now_str):
    """
    Shared 3-step escalation: step1 (MOO for the AM sweep, plain market for
    the PM sweep) -> aggressive limit at bid/last -> plain market order that
    fills at whatever the prevailing price is. Returns (total_filled_qty,
    total_notional, steps_used: list[str]) for logging/summary purposes.

    Each step only tries to sell whatever quantity is STILL unfilled from the
    previous step, so a partial fill at any stage is never double-sold.
    """
    remaining_qty = qty
    total_filled_qty = 0.0
    total_notional = 0.0
    steps_used = []

    def _log_fill(step_name, fq, fp):
        nonlocal total_filled_qty, total_notional
        if fq <= 0:
            return
        px = fp if fp else last_known_price
        total_filled_qty += fq
        total_notional += fq * px
        steps_used.append(f"{step_name}:{fq:.4f}sh@${px:.2f}")
        with open(csv_filename, mode='a', newline='') as f:
            csv.DictWriter(f, fieldnames=fieldnames).writerow({
                'Date': now_str, 'Buy': '', 'Sell': f"Sell ({sweep_label} {step_name})",
                'Quantity': fq, 'Symbol': symbol, 'Price Per Share': px,
            })

    # ---- Step 1: MOO (AM) or plain market (PM) ----
    # Defense-in-depth against Alpaca's real ~9:28am ET OPG cutoff: even
    # though the window check in _in_moo_sweep_window() already stops NEW
    # sweep runs after MOO_SWEEP_CUTOFF_SECS, a sweep already in progress
    # (e.g. slow on an earlier symbol in a long position list) could still
    # reach this point after the cutoff. If so, skip straight past the OPG
    # attempt -- which Alpaca would reject anyway -- to the limit fallback,
    # rather than wasting a submit/reject round trip this close to the open.
    skip_step1_late_opg = (
        step1_tif == 'opg'
        and datetime.now(eastern).replace(second=0, microsecond=0)
        >= datetime.now(eastern).replace(hour=MOO_SWEEP_HOUR, minute=MOO_SWEEP_MINUTE,
                                         second=0, microsecond=0) + timedelta(seconds=MOO_SWEEP_CUTOFF_SECS)
    )
    if skip_step1_late_opg:
        print(f"{symbol}: [{sweep_label}] past the safe OPG submission window "
              f"(cutoff ~9:28am ET); skipping straight to the limit fallback.")
    else:
        try:
            order = api.submit_order(symbol=symbol, qty=str(remaining_qty), side='sell',
                                     type=step1_type, time_in_force=step1_tif)
            print(f"{symbol}: [{sweep_label}] step 1 ({step1_type}/{step1_tif}) submitted "
                  f"for {remaining_qty:.4f} sh (order {getattr(order, 'id', 'n/a')}).")
            terminal, fq, fp, status = _poll_order_terminal(order.id, ESCALATION_STEP1_TIMEOUT_SECS)
            _log_fill('step1', fq, fp)
            remaining_qty = round(remaining_qty - fq, 6)
            if not terminal:
                print(f"{symbol}: [{sweep_label}] step 1 order not terminal after "
                      f"{ESCALATION_STEP1_TIMEOUT_SECS}s (status={status}); cancelling and escalating.")
                try:
                    api.cancel_order(order.id)
                    time.sleep(1)
                    # Pick up anything that filled in the instant before cancel landed.
                    o = api.get_order(order.id)
                    extra_fq = max(0.0, float(o.filled_qty or 0) - fq)
                    if extra_fq > 0:
                        _log_fill('step1_late', extra_fq, float(o.filled_avg_price) if o.filled_avg_price else fp)
                        remaining_qty = round(remaining_qty - extra_fq, 6)
                except Exception as e:
                    logging.warning(f"{symbol}: [{sweep_label}] step 1 cancel/recheck failed: {e}")
            elif status in ('canceled', 'expired', 'rejected') and fq == 0:
                print(f"{symbol}: [{sweep_label}] step 1 order {status} with no fill; escalating.")
        except Exception as e:
            print(f"{symbol}: [{sweep_label}] step 1 submit failed ({e}); escalating to limit fallback.")
            logging.error(f"{now_str} [{sweep_label}] {symbol} step 1 submit failed: {e}")

    if remaining_qty <= 0:
        return total_filled_qty, total_notional, steps_used

    # ---- Step 2: aggressive limit at bid/last ----
    limit_price = _get_bid_or_last_price(symbol, last_known_price)
    if LIMIT_FALLBACK_DISCOUNT_PCT > 0:
        limit_price = round(limit_price * (1 - LIMIT_FALLBACK_DISCOUNT_PCT), 2)
    try:
        order2 = api.submit_order(symbol=symbol, qty=str(remaining_qty), side='sell',
                                  type='limit', limit_price=str(round(limit_price, 2)),
                                  time_in_force='day')
        print(f"{symbol}: [{sweep_label}] step 2 (limit @ ${limit_price:.2f}) submitted "
              f"for {remaining_qty:.4f} sh (order {getattr(order2, 'id', 'n/a')}).")
        terminal2, fq2, fp2, status2 = _poll_order_terminal(order2.id, ESCALATION_STEP2_TIMEOUT_SECS)
        _log_fill('step2', fq2, fp2)
        remaining_qty = round(remaining_qty - fq2, 6)
        if not terminal2:
            print(f"{symbol}: [{sweep_label}] step 2 limit not terminal after "
                  f"{ESCALATION_STEP2_TIMEOUT_SECS}s (status={status2}); cancelling and escalating to market.")
            try:
                api.cancel_order(order2.id)
                time.sleep(1)
                o2 = api.get_order(order2.id)
                extra_fq2 = max(0.0, float(o2.filled_qty or 0) - fq2)
                if extra_fq2 > 0:
                    _log_fill('step2_late', extra_fq2, float(o2.filled_avg_price) if o2.filled_avg_price else fp2)
                    remaining_qty = round(remaining_qty - extra_fq2, 6)
            except Exception as e:
                logging.warning(f"{symbol}: [{sweep_label}] step 2 cancel/recheck failed: {e}")
        elif status2 in ('canceled', 'expired', 'rejected') and fq2 == 0:
            print(f"{symbol}: [{sweep_label}] step 2 limit {status2} with no fill; escalating to market.")
    except Exception as e:
        print(f"{symbol}: [{sweep_label}] step 2 submit failed ({e}); escalating to market fallback.")
        logging.error(f"{now_str} [{sweep_label}] {symbol} step 2 submit failed: {e}")

    if remaining_qty <= 0:
        return total_filled_qty, total_notional, steps_used

    # ---- Step 3: plain market order -- guarantees a fill at whatever the
    # prevailing price is (short of a trading halt or broker outage). This is
    # what makes the sweep "sell no matter what" rather than "try to sell". ----
    try:
        order3 = api.submit_order(symbol=symbol, qty=str(remaining_qty), side='sell',
                                  type='market', time_in_force='day')
        print(f"{symbol}: [{sweep_label}] step 3 (market) submitted for "
              f"{remaining_qty:.4f} sh (order {getattr(order3, 'id', 'n/a')}).")
        terminal3, fq3, fp3, status3 = _poll_order_terminal(order3.id, ESCALATION_STEP2_TIMEOUT_SECS)
        _log_fill('step3', fq3, fp3)
        remaining_qty = round(remaining_qty - fq3, 6)
        if remaining_qty > 0:
            # Still not fully sold -- most likely a trading halt or a broker/
            # network problem. Nothing further to escalate to; log loudly so
            # a human notices, since this is the edge case "100%" can't cover.
            print(f"{symbol}: [{sweep_label}] {RED}WARNING{RESET} {remaining_qty:.4f} sh "
                  f"still unsold after all 3 escalation steps (status={status3}). "
                  f"Likely a trading halt or broker issue -- needs manual attention.")
            logging.error(f"{now_str} [{sweep_label}] {symbol}: {remaining_qty:.4f} sh unsold "
                          f"after full escalation chain (final status={status3}).")
    except Exception as e:
        print(f"{symbol}: [{sweep_label}] step 3 (market) submit failed: {e}. "
              f"{remaining_qty:.4f} sh unsold -- needs manual attention.")
        logging.error(f"{now_str} [{sweep_label}] {symbol} step 3 submit failed: {e}. "
                      f"{remaining_qty:.4f} sh unsold.")

    return total_filled_qty, total_notional, steps_used


def _run_profit_sweep(sweep_label, step1_type, step1_tif):
    """
    Shared body for both the pre-market (MOO) and pre-close (market) profit
    sweeps, run in two phases:

    Phase 1 (per-position): sell every position that is individually
    profitable, via the 3-step escalation chain. Unchanged from before.

    Phase 2 (portfolio-level): look at whatever positions are LEFT after
    phase 1. If their COMBINED unrealized P/L, divided by their combined cost
    basis, is >= PORTFOLIO_LIQUIDATION_MIN_PROFIT_PCT, sell all of them --
    winners and losers together -- because the portfolio nets a profit even
    though some individual legs don't. If the remainder doesn't clear the
    bar, phase 2 does nothing and those positions are left exactly as phase 1
    left them.
    """
    now = datetime.now(eastern)
    now_str = now.strftime("Eastern Time | %I:%M:%S %p | %m-%d-%Y |")
    print(f"\n==================== {sweep_label} ====================")
    try:
        positions = api.list_positions()
    except Exception as e:
        print(f"{sweep_label}: failed to fetch positions from broker: {e}")
        logging.error(f"{now_str} {sweep_label}: list_positions failed: {e}")
        return

    if not positions:
        print(f"{sweep_label}: no open positions.")
        print("=================================================================================\n")
        return

    # ---------------- Phase 1: per-position profitable sells ----------------
    submitted, skipped = [], []
    remaining_after_phase1 = []  # positions phase 1 did NOT sell (unprofitable or partial leftovers)

    for p in positions:
        symbol = p.symbol
        try:
            qty = float(p.qty)
            avg_entry = float(p.avg_entry_price)
            current_price = float(p.current_price) if getattr(p, 'current_price', None) else None
            if current_price is None:
                current_price = get_current_price(to_yf(symbol))

            # BUGFIX: a position with valid qty/avg_entry but an unconfirmed
            # current price used to be dropped from BOTH phase 1 and phase 2
            # entirely -- silently missing from the phase 2 cost-basis/market-
            # value totals, which understates (or overstates) the true
            # combined portfolio return. If qty/avg_entry are sane but price
            # lookup failed, still fold it into phase 2 using avg_entry as a
            # conservative (zero-gain) stand-in for its market value, so the
            # combined math isn't silently wrong -- it just can't be sold in
            # phase 1 without a live price.
            if qty <= 0 or avg_entry <= 0:
                skipped.append((symbol, "missing/invalid qty or avg entry price"))
                continue
            if current_price is None or current_price <= 0:
                skipped.append((symbol, "missing/invalid current price"))
                remaining_after_phase1.append((symbol, qty, avg_entry, avg_entry))
                continue

            gain_pct = (current_price - avg_entry) / avg_entry
            if gain_pct <= MOO_SWEEP_MIN_PROFIT_PCT:
                skipped.append((symbol, f"not profitable ({gain_pct*100:+.2f}%)"))
                remaining_after_phase1.append((symbol, qty, avg_entry, current_price))
                continue

            # Always cancel any pre-existing open sell orders on this symbol
            # first, so the sweep owns and can sell the FULL qty rather than
            # only whatever fraction wasn't already tied up in an older order.
            _cancel_existing_sell_orders(symbol, sweep_label, now_str)
            sweep_qty = round(qty, 6)
            if sweep_qty <= 0:
                skipped.append((symbol, "zero qty after cancelling pre-existing orders"))
                continue

            print(f"{symbol}: {GREEN}+{gain_pct*100:.2f}%{RESET} unrealized "
                  f"(avg ${avg_entry:.2f} -> last ${current_price:.2f}). "
                  f"Selling {sweep_qty:.4f} sh via escalation chain.")
            filled_qty, notional, steps = _sell_with_escalation(
                symbol, sweep_qty, current_price, step1_type, step1_tif, sweep_label, now_str)

            if filled_qty > 0:
                avg_fill_price = notional / filled_qty
                logging.info(f"{now_str} {sweep_label} sold {symbol}: {filled_qty:.4f} sh "
                            f"@ avg ${avg_fill_price:.2f} via [{', '.join(steps)}] "
                            f"(unrealized {gain_pct*100:+.2f}% at submit time).")
                submitted.append((symbol, filled_qty, gain_pct, steps))
                leftover_qty = round(sweep_qty - filled_qty, 6)
                if leftover_qty > 0:
                    # Escalation chain didn't fully clear this position (e.g. a
                    # halt) -- what's left is still an open position and is a
                    # candidate for phase 2's portfolio check.
                    remaining_after_phase1.append((symbol, leftover_qty, avg_entry, current_price))
            else:
                skipped.append((symbol, "escalation chain produced no fill (see warnings above)"))
                remaining_after_phase1.append((symbol, sweep_qty, avg_entry, current_price))
        except Exception as e:
            print(f"{symbol}: {sweep_label} failed: {e}")
            logging.error(f"{now_str} {sweep_label} failed for {symbol}: {e}")
            skipped.append((symbol, f"error: {e}"))

    try:
        session.commit()
    except Exception as e:
        logging.error(f"{now_str} {sweep_label}: DB commit failed: {e}")
        session.rollback()

    print(f"\n{sweep_label} phase 1 (per-position) summary: {len(submitted)} position(s) sold, "
          f"{len(skipped)} skipped.")
    for sym, fq, gp, steps in submitted:
        print(f"  SOLD  {sym}: {fq:.4f} sh, {gp*100:+.2f}% unrealized at submit time, "
              f"steps=[{', '.join(steps)}]")
    for sym, reason in skipped:
        print(f"  skipped    {sym}: {reason}")

    # ---------------- Phase 2: portfolio-level liquidation ----------------
    if USE_PORTFOLIO_LIQUIDATION_SWEEP and remaining_after_phase1:
        _run_portfolio_liquidation_phase(sweep_label, step1_type, step1_tif,
                                         remaining_after_phase1, now_str)

    print("=================================================================================\n")


def _run_portfolio_liquidation_phase(sweep_label, step1_type, step1_tif,
                                     remaining_positions, now_str):
    """
    Phase 2: given the positions phase 1 left untouched -- normally
    unprofitable/flat positions, but also any position phase 1 tried to sell
    and only partially filled (e.g. a halt cut the escalation chain short) --
    compute the COMBINED unrealized P/L versus COMBINED cost basis. If that ratio clears
    PORTFOLIO_LIQUIDATION_MIN_PROFIT_PCT, sell all of them via the same
    escalation chain used in phase 1. Otherwise, do nothing -- these stay
    open exactly as phase 1 left them.
    """
    total_cost_basis = 0.0
    total_market_value = 0.0
    for symbol, qty, avg_entry, current_price in remaining_positions:
        total_cost_basis += qty * avg_entry
        total_market_value += qty * current_price

    if total_cost_basis <= 0:
        return

    portfolio_gain_pct = (total_market_value - total_cost_basis) / total_cost_basis
    print(f"\n{sweep_label} phase 2 (portfolio-level) check: {len(remaining_positions)} "
          f"remaining position(s), combined cost basis ${total_cost_basis:,.2f}, "
          f"combined market value ${total_market_value:,.2f}, "
          f"combined unrealized {portfolio_gain_pct*100:+.2f}% "
          f"(threshold {PORTFOLIO_LIQUIDATION_MIN_PROFIT_PCT*100:.2f}%).")

    if portfolio_gain_pct < PORTFOLIO_LIQUIDATION_MIN_PROFIT_PCT:
        print(f"  Combined unrealized {portfolio_gain_pct*100:+.2f}% is below the "
              f"{PORTFOLIO_LIQUIDATION_MIN_PROFIT_PCT*100:.2f}% threshold. "
              f"Leaving remaining positions open (no forced sells).")
        return

    print(f"  {GREEN}Combined unrealized {portfolio_gain_pct*100:+.2f}% clears the "
          f"{PORTFOLIO_LIQUIDATION_MIN_PROFIT_PCT*100:.2f}% threshold.{RESET} "
          f"Liquidating all {len(remaining_positions)} remaining position(s), "
          f"winners and losers together, via the escalation chain.")

    liq_submitted, liq_skipped = [], []
    for symbol, qty, avg_entry, current_price in remaining_positions:
        try:
            liq_label = f"{sweep_label} portfolio-liq"
            _cancel_existing_sell_orders(symbol, liq_label, now_str)
            sell_qty = round(qty, 6)
            if sell_qty <= 0:
                liq_skipped.append((symbol, "zero qty after cancelling pre-existing orders"))
                continue

            leg_gain_pct = (current_price - avg_entry) / avg_entry if avg_entry else 0.0
            print(f"{symbol}: {leg_gain_pct*100:+.2f}% individually "
                  f"(avg ${avg_entry:.2f} -> last ${current_price:.2f}). "
                  f"Selling {sell_qty:.4f} sh as part of portfolio liquidation.")
            filled_qty, notional, steps = _sell_with_escalation(
                symbol, sell_qty, current_price, step1_type, step1_tif, liq_label, now_str)

            if filled_qty > 0:
                avg_fill_price = notional / filled_qty
                logging.info(f"{now_str} {sweep_label} portfolio-liq sold {symbol}: "
                            f"{filled_qty:.4f} sh @ avg ${avg_fill_price:.2f} via "
                            f"[{', '.join(steps)}] (individual leg {leg_gain_pct*100:+.2f}%, "
                            f"portfolio combined {portfolio_gain_pct*100:+.2f}% at trigger).")
                liq_submitted.append((symbol, filled_qty, leg_gain_pct, steps))
            else:
                liq_skipped.append((symbol, "escalation chain produced no fill (see warnings above)"))
        except Exception as e:
            print(f"{symbol}: portfolio liquidation failed: {e}")
            logging.error(f"{now_str} {sweep_label} portfolio-liq failed for {symbol}: {e}")
            liq_skipped.append((symbol, f"error: {e}"))

    try:
        session.commit()
    except Exception as e:
        logging.error(f"{now_str} {sweep_label}: portfolio-liq DB commit failed: {e}")
        session.rollback()

    print(f"\n{sweep_label} phase 2 (portfolio-level) summary: {len(liq_submitted)} position(s) "
          f"sold, {len(liq_skipped)} skipped.")
    for sym, fq, gp, steps in liq_submitted:
        print(f"  SOLD  {sym}: {fq:.4f} sh, {gp*100:+.2f}% individually, "
              f"steps=[{', '.join(steps)}]")
    for sym, reason in liq_skipped:
        print(f"  skipped    {sym}: {reason}")


def run_premarket_profit_sweep():
    """
    At 9:25am Eastern, sell every currently-profitable open position via a
    3-step escalation chain: MOO order first (fills at the opening auction),
    falling back to an aggressive limit at the bid, falling back to a plain
    market order that fills regardless of price. Runs once per trading day.

    Standalone from sell_stocks(): does not touch profit_monitor state,
    scale-out tracking, or the intraday exit logic.
    """
    global _moo_sweep_last_run_date
    if not USE_PREMARKET_PROFIT_SWEEP:
        return

    now = datetime.now(eastern)
    today_str = now.date().strftime("%Y-%m-%d")
    with _moo_sweep_lock:
        if _moo_sweep_last_run_date == today_str:
            return
        if not _in_moo_sweep_window(now):
            return
        _moo_sweep_last_run_date = today_str  # claim the day before any I/O

    _run_profit_sweep("Pre-Market Profit Sweep (9:25am ET)", step1_type='market', step1_tif='opg')


def run_close_profit_sweep():
    """
    At CLOSE_SWEEP_HOUR:CLOSE_SWEEP_MINUTE Eastern (default 3:45pm, 15 minutes
    before the close), sell every currently-profitable open position via the
    same 3-step escalation chain, starting with a plain market order (there is
    no market-on-close order type used here -- MOC has its own earlier
    submission cutoff and would arrive too close to CLOSE_SWEEP_MINUTE to be
    reliable) and falling back to limit, then market, exactly as the AM sweep
    does. Runs once per trading day, only during regular market hours (this
    is called from inside the main trading loop, not the closed-market wait
    loop, since 3:45pm is itself during the open session).
    """
    global _close_sweep_last_run_date
    if not USE_CLOSE_PROFIT_SWEEP:
        return

    now = datetime.now(eastern)
    today_str = now.date().strftime("%Y-%m-%d")
    with _close_sweep_lock:
        if _close_sweep_last_run_date == today_str:
            return
        if not _in_close_sweep_window(now):
            return
        _close_sweep_last_run_date = today_str  # claim the day before any I/O

    _run_profit_sweep("Pre-Close Profit Sweep (3:45pm ET)", step1_type='market', step1_tif='day')


def stop_if_stock_market_is_closed():
    nyse = mcal.get_calendar('NYSE')
    while True:
        current_datetime = datetime.now(eastern)
        current_time_str = current_datetime.strftime("%A, %B %d, %Y, %I:%M:%S %p")
        sched = nyse.schedule(start_date=current_datetime.date(), end_date=current_datetime.date())

        if not sched.empty:
            market_open = sched.iloc[0]['market_open'].astimezone(eastern)
            market_close = sched.iloc[0]['market_close'].astimezone(eastern)
            if market_open <= current_datetime <= market_close:
                print("Market is open. Proceeding with trading operations.")
                logging.info(f"{current_time_str}: Market is open.")
                return
            msg = f"Market is closed. Open hours: {market_open.strftime('%I:%M %p')} - {market_close.strftime('%I:%M %p')}"
            # Runs its own once-per-day/9:25am gate internally; safe to call
            # on every tick of this wait loop. Only fires on a real trading
            # day (sched is non-empty here), never on weekends/holidays.
            try:
                run_premarket_profit_sweep()
            except Exception as e:
                logging.error(f"{current_time_str}: pre-market profit sweep raised: {e}")
        else:
            msg = "Market is closed today (holiday or weekend)."

        print('''
        *********************************************************************************
        ************ Billionaire Buying Strategy Version ********************************
        *********************************************************************************
            2026 Edition of the Advanced Stock Market Trading Robot, Version 9
                        https://github.com/CodeProSpecialist
               Margin Account Rules Engine - No PDT Round-Trip Limits
        ''')
        print(f'Current date & time (Eastern Time): {current_time_str}')
        print(msg)
        print("Waiting until Stock Market Hours to begin the Stockbot Trading Program.\n")
        logging.info(f"{current_time_str}: {msg}")
        time.sleep(60)


def print_database_tables():
    if not PRINT_DATABASE:
        return
    print("\nTrade History In This Robot's Database:\n")
    print("Stock | Buy or Sell | Quantity | Avg. Price | Date \n")
    for record in session.query(TradeHistory).all():
        print(f"{record.symbols} | {record.action} | {record.quantity:.4f} | {record.price:.2f} | {record.date}")

    print("----------------------------------------------------------------\n")
    print("Positions in the Database To Sell On or After the Date Shown:\n")
    print("Stock | Quantity | Avg. Price | Date \n")
    for record in session.query(Position).all():
        cp = get_current_price(record.symbols)
        # BUGFIX: guard against None current price / zero avg price
        if cp is not None and record.avg_price:
            pct = ((cp - record.avg_price) / record.avg_price) * 100
            color = GREEN if pct >= 0 else RED
            print(f"{record.symbols} | {record.quantity:.4f} | {record.avg_price:.2f} | "
                  f"{record.purchase_date} | Price Change: {color}{pct:.2f}%{RESET}")
        else:
            print(f"{record.symbols} | {record.quantity:.4f} | {record.avg_price:.2f} | {record.purchase_date}")
    print("\n")


# ---------------- Learn from trade history (review item #7) ----------------
# ANALYZE_TRADE_HISTORY_EVERY_N_CYCLES controls how often the main loop runs
# this (it queries the whole trade_features table, so it's not free to run
# every 60s tick). Purely informational -- it prints findings but does not
# change live parameters. Feed the printed buckets into BUY_SCORE_THRESHOLD
# tuning or REGIME_SIGNAL_WEIGHTS manually, or extend this into an automatic
# adjustment once you trust the sample size.
ANALYZE_TRADE_HISTORY_EVERY_N_CYCLES = 30   # roughly every 30 minutes at 60s/cycle
MIN_TRADES_FOR_ANALYSIS = 10


def analyze_trade_history():
    """
    REVIEW ITEM #7: query closed trades from TradeFeatures and report which
    entry-feature combinations were actually associated with profitable
    outcomes -- buy score bucket, regime, RSI bucket, pattern, MACD state.
    """
    closed = session.query(TradeFeatures).filter(TradeFeatures.outcome_pct.isnot(None)).all()
    if len(closed) < MIN_TRADES_FOR_ANALYSIS:
        print(f"Trade-history analysis: only {len(closed)} closed trades on record "
              f"(need {MIN_TRADES_FOR_ANALYSIS}+). Skipping.")
        return

    def _bucket_stats(rows, keyfn, label):
        buckets = {}
        for r in rows:
            k = keyfn(r)
            if k is None:
                continue
            buckets.setdefault(k, []).append(r.outcome_pct)
        print(f"\n  By {label}:")
        for k, outcomes in sorted(buckets.items(), key=lambda kv: -np.mean(kv[1])):
            win_rate = sum(1 for o in outcomes if o > 0) / len(outcomes) * 100
            print(f"    {k}: n={len(outcomes)}, avg={np.mean(outcomes)*100:+.2f}%, "
                  f"win rate={win_rate:.0f}%")

    print(f"\n--- Trade History Analysis ({len(closed)} closed trades) ---")
    overall = [r.outcome_pct for r in closed]
    print(f"  Overall: avg={np.mean(overall)*100:+.2f}%, "
          f"win rate={sum(1 for o in overall if o > 0) / len(overall) * 100:.0f}%")

    _bucket_stats(closed, lambda r: r.regime, "regime")
    _bucket_stats(closed, lambda r: int(r.buy_score) if r.buy_score is not None else None, "buy score")
    _bucket_stats(closed, lambda r: r.candlestick_pattern, "candlestick pattern")
    _bucket_stats(closed, lambda r: 'macd_bullish' if r.macd_above_signal else 'macd_bearish', "MACD state")
    print("--- End analysis ---\n")


# ---------------- Data-driven expected return by score bucket (review item #4) ----------------
# The review correctly points out that `rank_score = score / atr_pct` treats
# `score` as if it WERE an expected return, when it's really just an
# arbitrary point total -- "a score of 7" has no inherent mathematical
# meaning as "7 units of profit". There's no way to build a true statistical
# expected-return model without a real backtest, which isn't something this
# bot can run on its own live-only trade history. What it CAN do is use its
# own accumulating TradeFeatures history as a (small, continuously-improving)
# empirical substitute: once enough closed trades exist at a given score
# level, use their ACTUAL average outcome_pct as the reward term instead of
# the raw score. Below MIN_TRADES_FOR_SCORE_REWARD_ESTIMATE samples at a
# given score, or with no history at all yet, this silently falls back to the
# raw score -- so a fresh bot behaves exactly as before, and the ranking only
# gets more grounded in real outcomes as trade history accumulates.
MIN_TRADES_FOR_SCORE_REWARD_ESTIMATE = 15
_score_reward_cache = {'date': None, 'buckets': {}}  # per-day cache; rebuilt once per trading day


def get_expected_return_by_score(score):
    """
    Returns the empirical average outcome_pct for closed trades whose
    buy_score rounds to `score`, or None if there isn't enough history yet at
    that score level (caller should fall back to the raw score in that case).
    Rebuilt at most once per trading day -- this is a slow-moving statistic,
    not something that needs a fresh DB query for every candidate in every
    scan cycle.
    """
    today_str = datetime.now(eastern).date().strftime("%Y-%m-%d")
    if _score_reward_cache['date'] != today_str:
        buckets = {}
        try:
            closed = session.query(TradeFeatures).filter(TradeFeatures.outcome_pct.isnot(None)).all()
            for r in closed:
                if r.buy_score is None:
                    continue
                buckets.setdefault(int(round(r.buy_score)), []).append(r.outcome_pct)
        except Exception as e:
            logging.warning(f"get_expected_return_by_score: history query failed: {e}")
        _score_reward_cache['date'] = today_str
        _score_reward_cache['buckets'] = buckets

    outcomes = _score_reward_cache['buckets'].get(int(round(score)))
    if outcomes is None or len(outcomes) < MIN_TRADES_FOR_SCORE_REWARD_ESTIMATE:
        return None
    return float(np.mean(outcomes))




# Periodically runs the bounded auto-adjustment pass (see AdaptiveParams
# above). This DOES auto-apply changes to live trading parameters -- but only
# within the guardrails documented on AdaptiveParams: minimum sample size,
# capped step size, hard bounds, and a full audit log of every decision.
ADAPT_EVERY_N_CYCLES_MAIN_LOOP = ADAPT_EVERY_N_CYCLES  # alias for clarity in main()


def run_adaptive_parameter_pass():
    adaptive_params.run_adjustment_pass()


def get_symbols_to_buy():
    try:
        with open('electricity-or-utility-stocks-to-buy-list.txt', 'r') as file:
            symbols = [to_yf(line) for line in file if line.strip()]
        if not symbols:
            print("\n****  Error: stocks-to-buy-list.txt contains no stock symbols.  ****\n")
        return symbols
    except FileNotFoundError:
        print("\n****  Error: File not found: electricity-or-utility-stocks-to-buy-list.txt  ****\n")
        return []


def remove_symbols_from_trade_list(symbol):
    """BUGFIX: normalize both sides before comparing so BRK.B/BRK-B match."""
    target = to_yf(symbol)
    try:
        with open('electricity-or-utility-stocks-to-buy-list.txt', 'r') as file:
            lines = file.readlines()
        with open('electricity-or-utility-stocks-to-buy-list.txt', 'w') as file:
            for line in lines:
                if line.strip() and to_yf(line) != target:
                    file.write(line)
        print(f"Removed {target} from trade list.")
    except FileNotFoundError:
        pass


# BUGFIX: this wrapper was also @limits decorated on top of get_cached_data and
# _fetch_current_price. Only _fetch_current_price actually touches the network,
# so it is the only layer that should consume the budget.
def get_current_price(symbols, retries=3):
    for attempt in range(retries):
        try:
            price = get_cached_data(symbols, 'current_price', _fetch_current_price, symbols)
            if price is not None:
                return price
        except Exception as e:
            logging.error(f"Retry {attempt + 1}/{retries} failed for {symbols}: {e}")
            time.sleep(2 ** attempt)
    return None


def _last_close(symbol):
    """BUGFIX: took a pre-built Ticker and called .history() on it directly,
    bypassing the rate gate. It is a fallback path, so on a bad day it fired for
    EVERY symbol -- doubling real yfinance traffic invisibly. Now gated."""
    try:
        h = yf_history(symbol, period='1d')
        if h.empty:
            return None
        return float(h['Close'].iloc[-1])
    except Exception:
        return None


def _fetch_current_price(symbols):
    # BUGFIX: was @limits decorated on its own counter AND held yf_lock across
    # every call, including the fallback. Rate limiting now lives in yf_gate
    # (one shared budget), and yf_lock is taken inside yf_history per request
    # rather than held across two sequential network calls.
    yf_symbol = to_yf(symbols)
    now = datetime.now(eastern)
    t = now.time()
    current_price = None
    try:
        if time2(4, 0) <= t < time2(20, 0):
            prepost = not (time2(9, 30) <= t < time2(16, 0))
            data = yf_history(symbols, period='1d', interval='1m', prepost=prepost)
            if not data.empty:
                current_price = float(data['Close'].iloc[-1])
        if current_price is None:
            current_price = _last_close(symbols)
    except Exception as e:
        logging.error(f"Error fetching current price for {yf_symbol}: {e}")
        current_price = _last_close(symbols)

    if current_price is None:
        logging.error(f"Failed to retrieve current price for {yf_symbol}.")
        return None
    return round(current_price, 4)


def _fetch_atr(sym):
    """BUGFIX: was a nested closure with NO rate limit of its own. All yfinance
    traffic now goes through the shared yf_gate instead of a per-function
    @limits counter, so the 60/min cap is real across the whole process."""
    yf_symbol = to_yf(sym)
    data = yf_history(sym, period='60d')
    try:
        if len(data) < 23:
            return None
        atr = talib.ATR(data['High'].values, data['Low'].values, data['Close'].values, timeperiod=22)
        val = atr[-1]
        # BUGFIX: reject NaN/zero ATR which produced div-by-zero position sizes
        if val is None or not np.isfinite(val) or val <= 0:
            return None
        return float(val)
    except Exception as e:
        logging.error(f"Error calculating ATR for {yf_symbol}: {e}")
        return None


# BUGFIX: same nesting problem -- this wrapper only delegates to get_cached_data,
# whose _fetch_atr does the real network call. Don't double-count the budget.
def get_average_true_range(symbols):
    return get_cached_data(symbols, 'atr', _fetch_atr, symbols)


def get_atr_high_price(sym):
    atr = get_average_true_range(sym)
    cp = get_current_price(sym)
    return round(cp + 0.40 * atr, 4) if cp and atr else None


def get_atr_low_price(sym):
    atr = get_average_true_range(sym)
    cp = get_current_price(sym)
    return round(cp - 0.10 * atr, 4) if cp and atr else None


def is_in_uptrend(symbols_to_buy):
    # BUGFIX: refetched a full 1y of daily bars EVERY cycle to compute a
    # 200-day SMA that moves once a day. Now cached for 30m (CACHE_TTLS).
    yf_symbol = to_yf(symbols_to_buy)

    def _fetch_sma(sym):
        h = yf_history(sym, period='1y')
        if h.empty or len(h) < 200:
            return None
        return float(talib.SMA(h['Close'].values, timeperiod=200)[-1])

    sma_200 = get_cached_data(symbols_to_buy, 'uptrend', _fetch_sma, symbols_to_buy)
    if sma_200 is None:
        return False
    cp = get_current_price(symbols_to_buy)
    if cp is None or not np.isfinite(sma_200):
        return False
    return cp > sma_200


def get_daily_rsi(symbols_to_buy):
    # BUGFIX: refetched 60d of daily bars every cycle for a daily RSI. Cached 30m.
    def _fetch_rsi(sym):
        h = yf_history(sym, period='60d', interval='1d')
        if h.empty or len(h) < 15:
            return None
        r = talib.RSI(h['Close'].values, timeperiod=14)[-1]
        return round(float(r), 2) if np.isfinite(r) else None

    return get_cached_data(symbols_to_buy, 'daily_rsi', _fetch_rsi, symbols_to_buy)


# ---------------- Multi-timeframe confirmation (review item #4) ----------------
# Require the daily trend (already gated by is_in_uptrend/get_daily_rsi), the
# 60-minute trend, and a 5-minute reversal signal to all agree bullish before
# buying. This is intended to cut false entries where the daily picture looks
# fine but the stock is actively falling on a shorter timeframe right now.
def get_60min_trend_bullish(symbol):
    """60m bars: bullish if price is above its own 20-bar (≈20h) SMA."""
    def _fetch(sym):
        h = yf_history(sym, period='5d', interval='60m')
        if h.empty or len(h) < 20:
            return None
        close = h['Close'].values
        sma20 = talib.SMA(close, timeperiod=20)[-1]
        if not np.isfinite(sma20):
            return None
        return bool(close[-1] > sma20)

    result = get_cached_data(symbol, 'mtf_60m', _fetch, symbol)
    # Missing/short history: don't block the trade on a data gap, but don't
    # count it as confirmation either -- treat as neutral-pass.
    return True if result is None else result


def get_5min_reversal_bullish(symbol):
    """5m bars: bullish if the latest 5m close ticked up from the prior bar
    (a simple reversal-in-progress check for the shortest timeframe)."""
    def _fetch(sym):
        h = yf_history(sym, period='1d', interval='5m')
        if h.empty or len(h) < 3:
            return None
        close = h['Close'].values
        return bool(close[-1] >= close[-2])

    result = get_cached_data(symbol, 'mtf_5m', _fetch, symbol)
    return True if result is None else result


def multi_timeframe_confirms_bullish(symbol):
    """Daily trend is already checked by the caller (is_in_uptrend + daily RSI
    gate) before this runs; here we require the 60m and 5m timeframes to agree."""
    return get_60min_trend_bullish(symbol) and get_5min_reversal_bullish(symbol)


# ---------------- Relative strength (review item #3) ----------------
def get_relative_strength(symbol, benchmark='SPY'):
    """
    20-day return of `symbol` minus 20-day return of `benchmark`. Positive
    means the stock has been outperforming the benchmark over that window --
    a supplementary scanner signal, not a hard gate.
    """
    def _fetch(sym):
        try:
            sym_h = yf_history(sym, period='30d', interval='1d')
            bench_h = yf_history(benchmark, period='30d', interval='1d')
            if len(sym_h) < 21 or len(bench_h) < 21:
                return None
            sym_ret = float(sym_h['Close'].iloc[-1] / sym_h['Close'].iloc[-21] - 1)
            bench_ret = float(bench_h['Close'].iloc[-1] / bench_h['Close'].iloc[-21] - 1)
            return round(sym_ret - bench_ret, 4)
        except Exception as e:
            logging.warning(f"Relative strength fetch failed for {sym}: {e}")
            return None

    return get_cached_data(symbol, 'relative_strength', _fetch, symbol)


# ---------------- Earnings-date filter (review item #3) ----------------
EARNINGS_BLACKOUT_DAYS = 2  # skip new buys within this many days of an earnings print


def days_until_next_earnings(symbol):
    """
    Returns days until the next known earnings date, or None if unknown.
    Uses yfinance's calendar endpoint, which is not rate-gated the same way as
    price history, but we still route it through the shared cache to avoid
    hammering it every cycle.
    """
    def _fetch(sym):
        try:
            yf_gate.acquire()
            with yf_lock:
                cal = yf.Ticker(to_yf(sym)).calendar
            if not cal:
                return None
            # yfinance returns either a dict with 'Earnings Date' (list of dates)
            # or a DataFrame depending on version; handle both defensively.
            edate = None
            if isinstance(cal, dict):
                dates = cal.get('Earnings Date')
                if dates:
                    edate = dates[0] if isinstance(dates, (list, tuple)) else dates
            else:
                try:
                    edate = cal.loc['Earnings Date'].iloc[0]
                except Exception:
                    edate = None
            if edate is None:
                return None
            if hasattr(edate, 'date'):
                edate = edate.date()
            days = (edate - datetime.now(eastern).date()).days
            return int(days)
        except Exception as e:
            logging.info(f"Earnings date lookup failed for {sym}: {e}")
            return None

    return get_cached_data(symbol, 'earnings_date', _fetch, symbol)


def is_within_earnings_blackout(symbol):
    days = days_until_next_earnings(symbol)
    if days is None:
        return False  # unknown: don't block the trade on missing data
    return 0 <= days <= EARNINGS_BLACKOUT_DAYS


def calculate_technical_indicators(symbols, lookback_days=90):
    yf_symbol = to_yf(symbols)
    hist = yf_history(symbols, period=f'{lookback_days}d')
    if hist.empty or len(hist) < 35:
        return hist
    hist['macd'], hist['signal'], _ = talib.MACD(hist['Close'].values, fastperiod=12, slowperiod=26, signalperiod=9)
    hist['rsi'] = talib.RSI(hist['Close'].values, timeperiod=14)
    hist['volume'] = hist['Volume']
    return hist


def print_technical_indicators(symbols, historical_data):
    if historical_data is None or historical_data.empty:
        return
    cols = [c for c in ['Close', 'macd', 'signal', 'rsi', 'volume'] if c in historical_data.columns]
    print(f"\nTechnical Indicators for {symbols}:\n")
    print(historical_data[cols].tail())
    print("")


def get_previous_price(symbols):
    if symbols in previous_prices:
        return previous_prices[symbols]
    cp = get_current_price(symbols)
    if cp is not None:
        previous_prices[symbols] = cp
    return cp


def update_previous_price(symbols, current_price):
    if current_price is not None:
        previous_prices[symbols] = current_price


# ---------------- 2026 Margin rules engine ----------------
def get_margin_state():
    """Replaces PDT checks with margin-account health metrics."""
    acct = api.get_account()
    equity = float(acct.equity)
    last_equity = float(acct.last_equity or equity)
    cash = float(acct.cash)
    buying_power = float(acct.buying_power)
    dt_bp = float(getattr(acct, 'daytrading_buying_power', 0) or 0)
    rt_bp = float(getattr(acct, 'regt_buying_power', 0) or 0)
    long_mv = float(getattr(acct, 'long_market_value', 0) or 0)
    maint = float(getattr(acct, 'maintenance_margin', 0) or 0)

    # Effective purchasing power under our own leverage cap, not FINRA's PDT rule.
    if ACCOUNT_MODE == 'margin':
        effective_bp = min(buying_power, equity * MAX_LEVERAGE)
    else:
        effective_bp = min(cash, equity * MAX_LEVERAGE)

    margin_ratio = (equity / long_mv) if long_mv > 0 else 1.0
    healthy = margin_ratio >= MAINTENANCE_MARGIN_FLOOR_PCT

    return {
        'equity': equity, 'last_equity': last_equity, 'cash': cash,
        'buying_power': buying_power, 'daytrading_buying_power': dt_bp,
        'regt_buying_power': rt_bp, 'long_market_value': long_mv,
        'maintenance_margin': maint, 'effective_bp': effective_bp,
        'margin_ratio': margin_ratio, 'healthy': healthy,
        'trading_blocked': bool(acct.trading_blocked),
        'account_blocked': bool(acct.account_blocked),
    }


def day_trades_allowed():
    """2026 rules: no PDT round-trip counter. Only broker-level blocks matter."""
    if UNLIMITED_DAY_TRADES:
        st = get_margin_state()
        return not (st['trading_blocked'] or st['account_blocked'])
    return True


# ---------------- Open design questions (documented, not changed) ----------------
# A code review raised several points that are genuine judgment calls about
# how the strategy should work, not bugs -- changing them without a backtest
# would be guessing at new behavior rather than fixing broken behavior. Noted
# here so the reasoning isn't lost, without silently acting on it:
#
# Item 3 (scoring quantitativeness): the buy score combines RSI-below-50,
# rising MACD, a bullish 60m trend, and a bullish 5m tick into one additive
# point total. The reviewer called this a coherent CONCEPT (long-term
# bullish + short-term oversold + beginning to reverse) but said the
# implementation should be "more quantitative" than a collection of loosely
# related indicators accumulating points -- e.g. a proper multi-factor model
# with fitted/backtested weights instead of hand-picked integers. The
# regime-weighted scoring (REGIME_SIGNAL_WEIGHTS) is a step toward that, and
# the AdaptiveParams auto-tuner now nudges per-signal weights based on this
# bot's own trade outcomes -- but neither is a real quantitative model, and
# building one properly needs historical backtesting data this bot doesn't
# have access to on its own.
#
# Item 8 (profit-monitor tightness / parameter choices in general): the
# reviewer's specific, actionable suggestion here (giveback should scale with
# peak gain, not just the arm threshold) IS implemented -- see
# PEAK_GIVEBACK_FRACTION and ProfitMonitorEngine._giveback_for_peak(). The
# broader point -- that ARM/GIVEBACK/FLOOR defaults are "extremely tight" and
# sacrifice larger moves -- is a return-vs-frequency tradeoff with no
# objectively correct answer; the current constants (ARM_PROFIT_PCT,
# PEAK_GIVEBACK_PCT, HARD_FLOOR_PCT near the top of this file) are left at
# their existing values rather than guessed wider, since "wider" is not
# unambiguously better without data on how it performs live.
#
# Item 10 (walk-forward validation): AdaptiveParams already does a bounded,
# guardrailed form of this for the buy-score threshold and signal weights
# (min sample size, capped step size, hard bounds -- see AdaptiveParams
# class). A full walk-forward optimization across the wider parameter space
# the reviewer describes (ARM/GIVEBACK/FLOOR, ATR multipliers, position
# sizing) would need a proper historical backtesting harness, which is a
# meaningfully larger project than a live-only bot can bootstrap from its own
# trade history alone.


def compute_buy_score(df, current_price, previous_price, last_price, regime=None, weights=None,
                      intraday_pullback_pct=None):
    """
    BUGFIX: score is computed once, in one place, from clean booleans.
    Previously `score` was accumulated in two disconnected blocks with
    contradictory thresholds (`< 3` then `>= 3` with a `< 4` message).

    REVIEW ITEM #1: signal contributions are now weighted by market regime
    instead of always adding a flat +1/+2. A caller can pass `regime`/`weights`
    explicitly; otherwise the current live regime is looked up.

    REVIEW ITEM #2 (dip measurement): `price_decline` below compares the
    current price to the latest DAILY close, which measures "below
    yesterday's close" rather than "pulled back from where it's been
    trading today" -- a stock that ran up then pulled back intraday (e.g.
    $100 -> $99.80 -> $99.95) can still count as a "decline" against
    yesterday's close even though it's no longer near its intraday low.
    `intraday_pullback_pct`, when the caller supplies it (see
    price_history-based calculation in buy_stocks), measures the pullback
    from the recent INTRADAY high instead and is blended in as an additional
    signal alongside the existing daily-close measurement, not a replacement
    for it -- both can fire independently and both contribute to the score.
    """
    close = df['Close'].values
    open_ = df['Open'].values
    high = df['High'].values
    low = df['Low'].values

    if weights is None:
        weights = get_regime_weights(regime)

    reasons = []
    score = 0

    # --- Candlestick bullish reversal detection (most recent bar only) ---
    pattern_funcs = {
        'Hammer': talib.CDLHAMMER,
        'Bullish Engulfing': talib.CDLENGULFING,
        'Morning Star': talib.CDLMORNINGSTAR,
        'Piercing Line': talib.CDLPIERCING,
        'Three White Soldiers': talib.CDL3WHITESOLDIERS,
        'Dragonfly Doji': talib.CDLDRAGONFLYDOJI,
        'Inverted Hammer': talib.CDLINVERTEDHAMMER,
        'Tweezer Bottom': talib.CDLMATCHINGLOW,
    }
    detected = []
    for name, fn in pattern_funcs.items():
        try:
            res = fn(open_, high, low, close)
            # BUGFIX: require a BULLISH (>0) signal. The original accepted
            # `!= 0`, which let bearish (-100) prints count as buy signals.
            if len(res) and res[-1] > 0:
                detected.append(name)
        except Exception:
            continue

    if detected:
        score += weights.get('pattern', 2)
        reasons.append(f"patterns={','.join(detected)}")

    # --- RSI ---
    rsi_series = talib.RSI(close, timeperiod=14)
    latest_rsi = float(rsi_series[-1]) if len(rsi_series) and np.isfinite(rsi_series[-1]) else None
    rsi_decrease = False
    recent_avg_rsi = prior_avg_rsi = 0.0
    if len(rsi_series) >= 10:
        recent = rsi_series[-5:][np.isfinite(rsi_series[-5:])]
        prior = rsi_series[-10:-5][np.isfinite(rsi_series[-10:-5])]
        if len(recent) and len(prior):
            recent_avg_rsi, prior_avg_rsi = float(np.mean(recent)), float(np.mean(prior))
            rsi_decrease = recent_avg_rsi < prior_avg_rsi
    if latest_rsi is not None and latest_rsi < 50:
        score += weights.get('rsi_below_50', 1)
        reasons.append(f"rsi={latest_rsi:.1f}<50")
    if rsi_decrease:
        score += weights.get('rsi_falling', 1)
        reasons.append("rsi_falling")

    # --- Volume ---
    recent_avg_volume = float(df['Volume'].iloc[-5:].mean()) if len(df) >= 5 else 0.0
    prior_avg_volume = float(df['Volume'].iloc[-10:-5].mean()) if len(df) >= 10 else recent_avg_volume
    volume_decrease = recent_avg_volume < prior_avg_volume if len(df) >= 10 else False
    if not volume_decrease:
        score += weights.get('volume_holding', 1)
        reasons.append("volume_holding")

    # --- MACD ---
    macd, macd_signal, _ = talib.MACD(close, fastperiod=12, slowperiod=26, signalperiod=9)
    macd_above_signal = False
    if len(macd) and np.isfinite(macd[-1]) and np.isfinite(macd_signal[-1]):
        macd_above_signal = macd[-1] > macd_signal[-1]
    if macd_above_signal:
        score += weights.get('macd_above_signal', 1)
        reasons.append("macd>signal")

    # --- Price decline (BUGFIX: numeric magnitude, not a bool compared to a price) ---
    decline_pct = (last_price - current_price) / last_price if last_price else 0.0
    price_decline = decline_pct >= 0.002
    if price_decline:
        score += weights.get('price_decline', 1)
        reasons.append(f"dip={decline_pct*100:.2f}%")

    # --- Intraday pullback from recent peak (review item #2, additional signal) ---
    # Distinct from price_decline above: this measures how far current_price
    # has pulled back from the highest price seen so far TODAY in this
    # symbol's rolling intraday history, not from yesterday's daily close.
    # A stock sitting well below today's high reads as a real intraday
    # pullback even on a day where it's still net positive vs. yesterday's
    # close (which price_decline alone would miss entirely).
    intraday_pullback = intraday_pullback_pct is not None and intraday_pullback_pct >= 0.002
    if intraday_pullback:
        score += weights.get('intraday_pullback', 1)
        reasons.append(f"intraday_pullback={intraday_pullback_pct*100:.2f}%")

    # --- Pattern-specific confirmations ---
    pattern_bonus_w = weights.get('pattern_bonus', 1)
    for p in detected:
        if p == 'Hammer' and latest_rsi is not None and latest_rsi < 35 and decline_pct >= 0.003:
            score += pattern_bonus_w
        elif p == 'Bullish Engulfing' and prior_avg_volume and recent_avg_volume > 1.5 * prior_avg_volume:
            score += pattern_bonus_w
        elif p == 'Morning Star' and latest_rsi is not None and latest_rsi < 40:
            score += pattern_bonus_w
        elif p == 'Piercing Line' and recent_avg_rsi and recent_avg_rsi < 40:
            score += pattern_bonus_w
        elif p == 'Three White Soldiers' and not volume_decrease:
            score += pattern_bonus_w
        elif p == 'Dragonfly Doji' and latest_rsi is not None and latest_rsi < 30:
            score += pattern_bonus_w
        elif p == 'Inverted Hammer' and rsi_decrease:
            score += pattern_bonus_w
        elif p == 'Tweezer Bottom' and latest_rsi is not None and latest_rsi < 40:
            score += pattern_bonus_w

    return {
        'score': score, 'detected': detected, 'reasons': reasons,
        'latest_rsi': latest_rsi, 'rsi_decrease': rsi_decrease,
        'volume_decrease': volume_decrease, 'macd_above_signal': macd_above_signal,
        'price_decline': price_decline, 'decline_pct': decline_pct,
        'intraday_pullback': intraday_pullback, 'intraday_pullback_pct': intraday_pullback_pct,
    }


# Fallback threshold used only if regime lookup fails entirely (see
# get_buy_score_threshold). The live threshold is now dynamic per-regime, held
# in AdaptiveParams and auto-adjusted within guardrails (see AdaptiveParams).
BUY_SCORE_THRESHOLD_DEFAULT = 4


def buy_stocks(symbols_to_buy_list, lock):
    print("Starting buy_stocks function...")
    if not symbols_to_buy_list:
        logging.info("No symbols to buy.")
        return

    # BUGFIX: carry qty per-symbol. Previously the DB write used a single
    # leaked `filled_qty` from the last loop iteration for EVERY position.
    filled_records = []  # (alpaca_symbol, yf_symbol, qty, price, date_str)

    st = get_margin_state()
    if st['trading_blocked'] or st['account_blocked']:
        print("Account is blocked by the broker. No buys.")
        return
    if not st['healthy']:
        print(f"Margin health low (equity/long_mv = {st['margin_ratio']:.2f} < "
              f"{MAINTENANCE_MARGIN_FLOOR_PCT:.2f}). No new buys.")
        logging.warning("Margin maintenance floor breached; buys suspended.")
        return

    total_equity = st['equity']
    current_exposure = st['long_market_value']
    max_new_exposure = min(
        total_equity * MAX_PORTFOLIO_EXPOSURE_PCT - current_exposure,
        st['effective_bp'] - CASH_BUFFER,
    )
    if max_new_exposure <= MIN_ORDER_NOTIONAL:
        print("Exposure / buying-power limit reached. No new buys.")
        return
    print(f"Equity ${total_equity:,.2f} | Exposure ${current_exposure:,.2f} | "
          f"Effective BP ${st['effective_bp']:,.2f} | Headroom ${max_new_exposure:,.2f}")

    today_date_str = datetime.now(eastern).date().strftime("%Y-%m-%d")

    # ---------------- Regime + dynamic threshold (review items #6, #8) ----------------
    regime_info = get_market_regime()
    regime = regime_info['regime']
    dynamic_threshold = get_buy_score_threshold(regime)
    regime_weights = get_regime_weights(regime)
    vix_str = f"{regime_info['vix']:.1f}" if regime_info['vix'] is not None else "n/a"
    print(f"Market regime: {regime.upper()} (VIX {vix_str}) -> buy score threshold {dynamic_threshold}")

    # ---------------- Phase 1: scan and rank all candidates (review item #9) ----------------
    # Score every symbol first WITHOUT buying, then only submit orders for the
    # top-ranked candidates that fit within the available headroom. Ranking key
    # is expected-reward / expected-risk: buy score (reward proxy) divided by
    # ATR% (risk proxy), so a high-score low-volatility setup ranks above an
    # equally-scored but much choppier one.
    candidates = []  # list of dicts with everything buy execution needs

    for symbol in list(symbols_to_buy_list):
        yf_symbol = to_yf(symbol)
        api_symbol = to_alpaca(symbol)
        now_str = datetime.now(eastern).strftime("Eastern Time | %I:%M:%S %p | %m-%d-%Y |")

        # BUGFIX: per-symbol claim. Without it, buy_stocks could fill and add a
        # position for a symbol that sell_stocks was concurrently deciding to
        # exit, and both threads would race on the same broker position.
        if not position_book.claim(api_symbol):
            print(f"{yf_symbol}: busy in another thread this cycle. Skipping.")
            continue
        release_now = True
        try:
            current_price = get_current_price(symbol)
            if current_price is None or current_price <= 0:
                continue

            # Track rolling price history
            ts = time.time()
            if symbol not in price_history:
                price_history[symbol] = {i: [] for i in interval_map}
                last_stored[symbol] = {i: 0 for i in interval_map}
            for interval, delta in interval_map.items():
                if ts - last_stored[symbol][interval] >= delta:
                    price_history[symbol][interval].append(current_price)
                    price_history[symbol][interval] = price_history[symbol][interval][-50:]
                    last_stored[symbol][interval] = ts

            # NOTE: the 90d candle fetch used to happen here, before the SMA/RSI
            # gates below. It now runs only for symbols that survive them.

            # --- Trend + multi-timeframe filters ---
            # PERF: these run BEFORE the 90d candle fetch. Both are cached for
            # 30m, so on a warm cache they cost zero yfinance requests and reject
            # most symbols for free. Fetching the 90d history first (as before)
            # meant paying a request for every symbol that was about to be cut.
            if not is_in_uptrend(symbol):
                print(f"{yf_symbol}: below 200-day SMA. Skipping.")
                update_previous_price(symbol, current_price)
                continue

            daily_rsi = get_daily_rsi(symbol)
            if daily_rsi is None or daily_rsi > 50:
                print(f"{yf_symbol}: daily RSI not oversold ({daily_rsi}). Skipping.")
                update_previous_price(symbol, current_price)
                continue

            # REVIEW ITEM #4: multi-timeframe confirmation. Daily trend/RSI just
            # passed above; also require the 60m and 5m timeframes to agree
            # bullish before spending a request on the 90d candle history.
            if not multi_timeframe_confirms_bullish(symbol):
                print(f"{yf_symbol}: multi-timeframe confirmation failed (60m/5m not bullish). Skipping.")
                update_previous_price(symbol, current_price)
                continue

            # REVIEW ITEM #3: earnings blackout. Avoid opening new positions
            # right before/after an earnings print, which can gap through any
            # stop or profit-monitor logic.
            if is_within_earnings_blackout(symbol):
                print(f"{yf_symbol}: within {EARNINGS_BLACKOUT_DAYS}-day earnings blackout. Skipping.")
                update_previous_price(symbol, current_price)
                continue

            # Only survivors pay for the 90d candle history.
            df = get_cached_data(symbol, 'history_90d',
                                 lambda s: yf_history(s, period="90d"), symbol)
            # BUGFIX: MACD(26,9) and RSI(14) need ~35 bars. Original required
            # only 3 rows, producing all-NaN indicators that silently scored 0.
            if df.empty or len(df) < 40:
                print(f"{yf_symbol}: insufficient history ({len(df)} bars). Skipping.")
                continue

            previous_price = get_previous_price(symbol) or current_price
            last_price = float(df['Close'].iloc[-1])

            # REVIEW ITEM #2: intraday pullback from the recent intraday HIGH,
            # using the rolling 1-minute price history already being tracked
            # above -- distinct from price_decline in compute_buy_score, which
            # only compares against yesterday's daily close. Falls back to
            # None (signal simply doesn't fire) if there isn't enough intraday
            # history yet for this symbol today.
            intraday_samples = price_history.get(symbol, {}).get('1min', [])
            intraday_pullback_pct = None
            if len(intraday_samples) >= 3:
                intraday_high = max(intraday_samples)
                if intraday_high > 0:
                    intraday_pullback_pct = (intraday_high - current_price) / intraday_high

            sig = compute_buy_score(df, current_price, previous_price, last_price,
                                    regime=regime, weights=regime_weights,
                                    intraday_pullback_pct=intraday_pullback_pct)

            # Price-stability bonus (BUGFIX: defined unconditionally, so the
            # `else` logging branch can no longer raise NameError)
            price_stable = True
            hist5 = price_history.get(symbol, {}).get('5min', [])
            if len(hist5) >= 2 and hist5[-2]:
                price_stable = abs(hist5[-1] - hist5[-2]) / hist5[-2] < 0.005
                if price_stable:
                    sig['score'] += regime_weights.get('price_stable', 1)

            # REVIEW ITEM #3: relative strength vs SPY is added as a supplementary
            # scanner signal (not a hard gate) -- outperformance nudges the score.
            rel_strength = get_relative_strength(symbol)
            if rel_strength is not None and rel_strength > 0:
                sig['score'] += 1
                sig['reasons'].append(f"rs_vs_spy=+{rel_strength*100:.2f}%")

            # ATR is needed both for the ML feature vector below and for
            # ranking further down -- fetched once here (get_average_true_range
            # is itself cached, so this isn't a double cost vs. the old layout).
            atr_for_rank = get_average_true_range(symbol)
            atr_pct = (atr_for_rank / current_price) if atr_for_rank and current_price else None

            # ML brain adjustment: a small +/- nudge from the inlined
            # TensorFlow model above, trained on real historical market data
            # (train_ml_brain_from_historical_data) and fine-tuned on this
            # bot's own closed-trade history (train_ml_brain_from_live_trades).
            # Returns None (no opinion, no adjustment applied) until enough
            # LIVE trade history exists to be minimally trustworthy for LIVE
            # decisions -- see ML_MIN_TRADES_FOR_LIVE_ADJUSTMENT. This is
            # additive, never a gate: it cannot by itself push a symbol above
            # or below the buy threshold in a way the rule-based score didn't
            # already get close to on its own (capped at +/-ML_MAX_SCORE_ADJUSTMENT).
            ml_adjustment = None
            if USE_ML_BRAIN_ADJUSTMENT and _ml_brain_is_available():
                try:
                    ml_adjustment = get_ml_adjustment(
                        session, TradeFeatures,
                        buy_score=sig['score'], rsi=sig.get('latest_rsi'),
                        atr_pct=atr_pct,
                        macd_above_signal=sig.get('macd_above_signal'),
                        volume_holding=not sig.get('volume_decrease'),
                        regime=regime,
                        # New sequence-model kwargs: the Conv1D->LSTM model
                        # needs a rolling window of daily bars, built from
                        # the same df compute_buy_score just used above.
                        symbol=symbol, df=df, current_price=current_price,
                    )
                except Exception as e:
                    logging.warning(f"{yf_symbol}: ml_brain adjustment failed: {e}")
                    ml_adjustment = None
                if ml_adjustment is not None:
                    sig['score'] += ml_adjustment
                    sig['reasons'].append(f"ml_brain={ml_adjustment:+.2f}")

            if not sig['detected']:
                print(f"{yf_symbol}: no bullish reversal pattern. Score {sig['score']}. Skipping.")
                update_previous_price(symbol, current_price)
                continue

            if sig['score'] < dynamic_threshold:
                print(f"{yf_symbol}: score {sig['score']} < {dynamic_threshold} ({regime} regime). "
                      f"[{'; '.join(sig['reasons'])}] Skipping.")
                logging.info(f"{now_str} Skipped {yf_symbol}: score {sig['score']} (threshold {dynamic_threshold})")
                update_previous_price(symbol, current_price)
                continue

            # REVIEW ITEM #4: rank by an empirical expected-return estimate
            # when enough of this bot's own trade history exists at this
            # score level; otherwise fall back to the raw score, exactly as
            # before. Either way this is divided by ATR% as the risk proxy.
            expected_return = get_expected_return_by_score(sig['score'])
            reward_term = expected_return if expected_return is not None else sig['score']
            reward_source = 'history' if expected_return is not None else 'score'
            # A negative empirical expected return at this score level should
            # never rank ABOVE a positive one just because ATR% divides it
            # toward zero -- floor it at a small positive epsilon so a
            # historically-losing score bucket sinks to the bottom of the
            # ranking instead of being inflated by a low-volatility stock.
            reward_term = max(reward_term, 0.0001) if reward_source == 'history' else reward_term
            rank_score = reward_term / max(atr_pct, 0.001) if atr_pct else reward_term

            candidates.append({
                'symbol': symbol, 'yf_symbol': yf_symbol, 'api_symbol': api_symbol,
                'current_price': current_price, 'sig': sig, 'atr': atr_for_rank,
                'atr_pct': atr_pct, 'rank_score': rank_score, 'regime': regime,
                'reward_source': reward_source, 'expected_return': expected_return,
            })
            update_previous_price(symbol, current_price)
            # Keep this symbol's claim held through phase 2 -- released below
            # once phase 2 decides whether to buy it.
            release_now = False
        finally:
            if release_now:
                position_book.release(api_symbol)

    if not candidates:
        print("No candidates passed the scan/rank filters this cycle.")
        return

    # Rank best-first. Only the top N are actually bought (review item #9): the
    # rest are released so they don't sit locked out of a future cycle.
    candidates.sort(key=lambda c: c['rank_score'], reverse=True)
    to_buy = candidates[:MAX_NEW_POSITIONS_PER_CYCLE]
    skipped = candidates[MAX_NEW_POSITIONS_PER_CYCLE:]
    for c in skipped:
        print(f"{c['yf_symbol']}: ranked #{candidates.index(c)+1} of {len(candidates)} "
              f"(score {c['sig']['score']}, rank {c['rank_score']:.1f}) — outside top "
              f"{MAX_NEW_POSITIONS_PER_CYCLE}. Not buying this cycle.")
        position_book.release(c['api_symbol'])

    if to_buy:
        print(f"Ranked buy candidates ({len(to_buy)} of {len(candidates)}):")
        for i, c in enumerate(to_buy, 1):
            atr_pct_str = f"{c['atr_pct']*100:.2f}%" if c['atr_pct'] else "n/a"
            if c['reward_source'] == 'history':
                reward_str = f"empirical avg {c['expected_return']*100:+.2f}%"
            else:
                reward_str = f"raw score {c['sig']['score']} (insufficient trade history yet)"
            print(f"  #{i} {c['yf_symbol']}: {reward_str}, ATR% {atr_pct_str}, "
                  f"rank {c['rank_score']:.1f}")

    # ---------------- Phase 2: execute buys for the ranked shortlist ----------------
    for c in to_buy:
        symbol, yf_symbol, api_symbol = c['symbol'], c['yf_symbol'], c['api_symbol']
        current_price, sig = c['current_price'], c['sig']
        now_str = datetime.now(eastern).strftime("Eastern Time | %I:%M:%S %p | %m-%d-%Y |")
        try:
            # ---------------- Position sizing ----------------
            if ALL_BUY_ORDERS_ARE_1_DOLLAR:
                notional = MIN_ORDER_NOTIONAL
            else:
                atr = get_average_true_range(symbol)
                if atr is None:
                    print(f"{yf_symbol}: no valid ATR. Skipping.")
                    continue
                # RISK ALIGNMENT FIX (review item #5): risk_per_share now uses the
                # SAME multiplier the hard stop-loss actually enforces
                # (HARD_STOP_ATR_MULTIPLIER), so the 1%-of-equity risk this sizing
                # targets is the position's REAL maximum loss, not a distance no
                # stop was ever placed at.
                risk_per_share = HARD_STOP_ATR_MULTIPLIER * atr
                risk_amount = RISK_PER_TRADE_PCT * total_equity
                notional = (risk_amount / risk_per_share) * current_price

                with lock:
                    cash_available = float(api.get_account().cash)
                headroom = min(
                    MAX_ALLOCATION_PER_SYMBOL,
                    max_new_exposure,
                    (cash_available - CASH_BUFFER) if ACCOUNT_MODE == 'cash' else max_new_exposure,
                )

                # BUGFIX: on small accounts, risk-based sizing always lands below the
                # broker's $1 notional floor, so every trade was silently discarded.
                # Round up to the floor when headroom allows; only skip if it doesn't.
                # NOTE: rounding up intentionally exceeds RISK_PER_TRADE_PCT. See
                # MIN_EQUITY_TO_TRADE if you'd rather halt than over-risk.
                if notional < MIN_ORDER_NOTIONAL:
                    if headroom >= MIN_ORDER_NOTIONAL:
                        actual_risk_pct = (MIN_ORDER_NOTIONAL / current_price * risk_per_share) / total_equity * 100
                        print(f"{yf_symbol}: risk-sized ${notional:.2f} < ${MIN_ORDER_NOTIONAL:.2f} floor; "
                              f"rounding up (risk becomes {actual_risk_pct:.2f}% of equity)")
                        notional = MIN_ORDER_NOTIONAL
                    else:
                        print(f"{yf_symbol}: headroom ${headroom:.2f} < ${MIN_ORDER_NOTIONAL:.2f} minimum. Skipping.")
                        continue
                else:
                    # Slippage haircut
                    notional = min(notional, headroom) * 0.999

            notional = round(notional, 2)
            if notional < MIN_ORDER_NOTIONAL:
                print(f"{yf_symbol}: notional ${notional:.2f} below ${MIN_ORDER_NOTIONAL:.2f} minimum. Skipping.")
                continue

            with lock:
                bp = float(api.get_account().buying_power)
            if bp < notional + CASH_BUFFER:
                print(f"{yf_symbol}: insufficient buying power (${bp:.2f} < ${notional + CASH_BUFFER:.2f}).")
                continue

            if not day_trades_allowed():
                print("Broker has blocked trading on this account.")
                break

            qty_est = round(notional / current_price, 4)
            reason = f"score={sig['score']} [{'; '.join(sig['reasons'])}]"
            print(f"Submitting buy: {api_symbol} ~{qty_est:.4f} sh @ ${current_price:.2f} "
                  f"(notional ${notional:.2f}) | {reason}")

            try:
                buy_order = api.submit_order(
                    symbol=api_symbol,
                    notional=notional,
                    side='buy',
                    type='market',
                    time_in_force='day',
                )
                logging.info(f"{now_str} Submitted buy {api_symbol} notional ${notional:.2f}: {reason}")

                filled_qty = 0.0
                filled_price = current_price
                terminal = False
                for _ in range(30):
                    try:
                        o = api.get_order(buy_order.id)
                    except Exception as e:
                        # BUGFIX: a transient network error during polling used to
                        # escape the APIError handler and kill the whole buy loop,
                        # skipping the DB persist for every prior fill in this pass.
                        logging.warning(f"{api_symbol}: poll error ({e}); retrying.")
                        time.sleep(2)
                        continue

                    # BUGFIX: track partial fills. The old code only broke on exactly
                    # 'filled', so a partially_filled order polled out and was logged
                    # as "not filled" -- while the shares were actually owned, with no
                    # DB row, no stop, and no trade history. Silent orphan position.
                    filled_qty = float(o.filled_qty or 0)
                    if o.filled_avg_price:
                        filled_price = float(o.filled_avg_price)

                    if o.status == 'filled':
                        terminal = True
                        break
                    if o.status in ('canceled', 'expired', 'rejected'):
                        print(f"{api_symbol}: order {o.status} (filled {filled_qty:.4f} before stopping).")
                        logging.warning(f"{api_symbol}: order {o.status}, partial qty {filled_qty:.4f}.")
                        terminal = True
                        break
                    time.sleep(2)

                # BUGFIX: cancel a still-open order that never reached a terminal
                # state, so it can't fill later behind our back and leave the broker
                # holding shares this bot has no record of.
                if not terminal:
                    try:
                        api.cancel_order(buy_order.id)
                        logging.warning(f"{api_symbol}: buy order timed out after 60s; cancel requested.")
                        print(f"{api_symbol}: order timed out, cancel requested.")
                        time.sleep(2)
                        o = api.get_order(buy_order.id)
                        filled_qty = float(o.filled_qty or 0)
                        if o.filled_avg_price:
                            filled_price = float(o.filled_avg_price)
                    except Exception as e:
                        logging.error(f"{api_symbol}: cancel/re-check failed: {e}")

                # Any qty actually acquired is recorded, whether the order completed
                # fully, partially, or was cancelled mid-flight.
                if filled_qty > 0:
                    print(f"Filled {filled_qty:.4f} sh of {api_symbol} @ "
                          f"{GREEN}${filled_price:.2f}{RESET} (cost ${filled_qty * filled_price:.2f})")
                    with open(csv_filename, mode='a', newline='') as f:
                        csv.DictWriter(f, fieldnames=fieldnames).writerow({
                            'Date': now_str, 'Buy': 'Buy', 'Sell': '',
                            'Quantity': filled_qty, 'Symbol': api_symbol,
                            'Price Per Share': filled_price,
                        })
                    filled_records.append((api_symbol, yf_symbol, filled_qty, filled_price, today_date_str, sig))

                    if USE_TRAILING_STOP and not ALL_BUY_ORDERS_ARE_1_DOLLAR:
                        sid = place_trailing_stop_sell_order(api_symbol, filled_qty, filled_price)
                        print(f"Trailing stop for {api_symbol}: {sid or 'not placed (see log)'}")
                else:
                    print(f"Buy order not filled for {api_symbol}")
                    logging.info(f"{now_str} Buy order not filled for {api_symbol}")

            except tradeapi.rest.APIError as e:
                print(f"Error submitting buy order for {api_symbol}: {e}")
                logging.error(f"Error submitting buy order for {api_symbol}: {e}")
                continue
            except Exception as e:
                # BUGFIX: catch-all so one unexpected failure can't abort the loop
                # and discard already-filled records awaiting persist.
                print(f"Unexpected error handling buy for {api_symbol}: {e}")
                logging.error(f"Unexpected error handling buy for {api_symbol}: {e}")
                continue

            update_previous_price(symbol, current_price)
            time.sleep(0.8)
        finally:
            # BUGFIX: always release, including on every `continue` path and on
            # exception, or the symbol stays locked out of trading forever.
            position_book.release(api_symbol)

    # ---------------- Persist fills ----------------
    if not filled_records:
        return
    try:
        with lock:
            for api_symbol, yf_symbol, qty, price, dstr, sig in filled_records:
                # BUGFIX: mutate the shared PositionBook in place instead of a
                # by-reference dict that refresh_* would later rebind away.
                position_book.upsert(api_symbol, round(price, 4), dstr)
                if yf_symbol in symbols_to_buy_list:
                    symbols_to_buy_list.remove(yf_symbol)   # BUGFIX: guarded remove
                remove_symbols_from_trade_list(yf_symbol)

                session.add(TradeHistory(symbols=api_symbol, action='buy',
                                         quantity=qty, price=price, date=dstr))

                # REVIEW ITEM #7: snapshot the features present at entry so
                # they can later be joined against the eventual outcome.
                atr_val = get_average_true_range(yf_symbol)
                session.add(TradeFeatures(
                    symbols=api_symbol, entry_date=dstr, entry_price=price,
                    rsi=sig.get('latest_rsi'),
                    macd_above_signal=int(bool(sig.get('macd_above_signal'))),
                    atr_pct=(atr_val / price) if atr_val and price else None,
                    volume_holding=int(bool(sig.get('volume_decrease')) is False),
                    candlestick_pattern=','.join(sig.get('detected', [])) or None,
                    buy_score=sig.get('score'),
                    regime=regime,
                    time_of_day=datetime.now(eastern).strftime('%H:%M'),
                ))
                # BUGFIX: merge instead of add — a re-buy of an existing symbol
                # previously raised an IntegrityError on the primary key.
                existing = session.query(Position).filter_by(symbols=api_symbol).one_or_none()
                if existing:
                    total_qty = existing.quantity + qty
                    existing.avg_price = ((existing.avg_price * existing.quantity) + (price * qty)) / total_qty
                    existing.quantity = total_qty
                    existing.purchase_date = dstr
                else:
                    session.add(Position(symbols=api_symbol, quantity=qty,
                                         avg_price=price, purchase_date=dstr))
            session.commit()
        print("Database updated successfully.")
    except SQLAlchemyError as e:
        session.rollback()
        print(f"Database error: {e}")
        logging.error(f"Database error: {e}")
        return

    # BUGFIX: refresh_after_buy() used to run INSIDE `with lock`. It sleeps 2s
    # and then makes blocking API calls (list_positions plus a paginated order
    # lookup per symbol), holding the mutex for tens of seconds and serializing
    # both worker threads. Now called after the lock is released.
    refresh_after_buy()


def refresh_after_buy():
    # BUGFIX: no longer rebinds globals. symbols_to_buy is refreshed by main()
    # each cycle, and the position view is mutated in place so the other thread
    # keeps seeing the same object.
    time.sleep(2)
    position_book.replace_all(update_symbols_to_sell_from_api())


@sleep_and_retry
@limits(calls=CALLS, period=PERIOD)
def place_trailing_stop_sell_order(symbol, qty, current_price, retries=3):
    """
    Places a trailing stop on the whole-share portion. Alpaca does not accept
    fractional qty for trailing_stop orders, so any fractional remainder is left
    to the sell_stocks take-profit logic.

    BUGFIX: previously a failed stop just printed and moved on, leaving an
    unprotected position. Now retries with backoff and escalates on give-up.
    """
    whole = int(qty)
    if whole < 1:
        logging.info(f"{symbol}: qty {qty:.4f} < 1 whole share; trailing stop not supported "
                     f"by broker for fractional qty. Managed by sell_stocks instead.")
        return None

    for attempt in range(retries):
        try:
            stop_order = api.submit_order(
                symbol=symbol,
                qty=whole,
                side='sell',
                type='trailing_stop',
                trail_percent=str(TRAIL_PERCENT),
                time_in_force='gtc',
            )
            logging.info(f"Placed trailing stop ({TRAIL_PERCENT}%) for {whole} sh of {symbol}: {stop_order.id}")
            return stop_order.id
        except Exception as e:
            logging.error(f"Trailing stop attempt {attempt + 1}/{retries} failed for {symbol}: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)

    # Give-up path: the position is live and unprotected. Make that loud.
    msg = (f"CRITICAL: could not place trailing stop for {whole} sh of {symbol} after "
           f"{retries} attempts. POSITION IS UNPROTECTED - exit relies on take-profit only.")
    print(f"{RED}{msg}{RESET}")
    logging.critical(msg)
    return None


@sleep_and_retry
@limits(calls=CALLS, period=PERIOD)
def cancel_open_sell_orders(symbol):
    """
    Cancel resting sell orders (e.g. the GTC trailing stop) so a take-profit can
    sell the full position.

    BUGFIX: no cancel logic existed at all. A GTC trailing stop reserves shares
    at the broker, so sell_stocks could only ever offload the unreserved
    fraction -- the whole-share portion could never exit on the profit target.

    Returns True if it is safe to proceed with a full-size sell.
    """
    try:
        open_sells = [o for o in api.list_orders(status='open') if o.symbol == symbol and o.side == 'sell']
    except Exception as e:
        logging.error(f"{symbol}: could not list open orders: {e}")
        return False

    if not open_sells:
        return True

    for o in open_sells:
        try:
            api.cancel_order(o.id)
            logging.info(f"{symbol}: cancelled resting sell order {o.id} ({o.type}) to free shares.")
        except Exception as e:
            logging.error(f"{symbol}: failed to cancel sell order {o.id}: {e}")
            return False

    # Cancellation is asynchronous; wait for the broker to release the shares.
    for _ in range(10):
        time.sleep(1)
        try:
            still_open = [o for o in api.list_orders(status='open')
                          if o.symbol == symbol and o.side == 'sell']
            if not still_open:
                return True
        except Exception as e:
            logging.error(f"{symbol}: error confirming cancellation: {e}")
            return False

    logging.warning(f"{symbol}: sell orders still open after cancel; skipping this cycle.")
    return False


@sleep_and_retry
@limits(calls=CALLS, period=PERIOD)
def get_most_recent_purchase_date(symbol):
    try:
        order_list = []
        CHUNK_SIZE = 500
        until = datetime.now(pytz.UTC).isoformat()
        # BUGFIX: unbounded while-loop could paginate forever on a busy account.
        for _ in range(10):
            chunk = api.list_orders(status='all', nested=False, direction='desc',
                                    until=until, limit=CHUNK_SIZE, symbols=[symbol])
            if not chunk:
                break
            order_list.extend(chunk)
            until = (chunk[-1].submitted_at - timedelta(seconds=1)).isoformat()
            if len(chunk) < CHUNK_SIZE:
                break

        buys = [o for o in order_list if o.side == 'buy' and o.status == 'filled' and o.filled_at]
        if buys:
            d = max(buys, key=lambda o: o.filled_at).filled_at.date()
            return d.strftime("%Y-%m-%d")
    except Exception as e:
        logging.error(f"Error fetching buy orders for {symbol}: {e}")
    return datetime.now(eastern).date().strftime("%Y-%m-%d")


@sleep_and_retry
@limits(calls=CALLS, period=PERIOD)
def update_symbols_to_sell_from_api():
    positions = api.list_positions()
    d = {}
    live = set()
    for p in positions:
        sym = p.symbol
        live.add(sym)
        avg = float(p.avg_entry_price)
        qty = float(p.qty)
        pdate = get_most_recent_purchase_date(sym)
        row = session.query(Position).filter_by(symbols=sym).one_or_none()
        if row:
            row.quantity, row.avg_price, row.purchase_date = qty, avg, pdate
        else:
            session.add(Position(symbols=sym, quantity=qty, avg_price=avg, purchase_date=pdate))
        d[sym] = (avg, pdate)

    # BUGFIX: prune DB rows for positions that no longer exist at the broker,
    # otherwise sell_stocks kept trying to sell phantom holdings forever.
    for row in session.query(Position).all():
        if row.symbols not in live:
            session.delete(row)

    session.commit()
    return d


class PositionBook:
    """
    Thread-safe owner of the shared position view.

    BUGFIX (rebind race): main() passed `symbols_to_sell_dict` BY REFERENCE to
    both worker threads, and refresh_after_buy()/refresh_after_sell() then did
    `global symbols_to_sell_dict; symbols_to_sell_dict = {...}` -- REBINDING the
    global to a brand-new dict. The threads kept references to the OLD object,
    so every update after the first refresh was silently discarded and the two
    threads mutated different dicts. This class is never rebound; it mutates one
    dict in place under its own lock, so all readers see the same state.

    BUGFIX (per-symbol races): buy_stocks and sell_stocks could both act on the
    same symbol concurrently (a fill landing while sell was deciding to exit).
    claim()/release() give a per-symbol mutex so only one side touches a symbol
    at a time.
    """

    def __init__(self):
        self._data = {}                       # symbol -> (avg_price, purchase_date)
        self._lock = threading.RLock()
        self._claims = set()                  # symbols currently owned by a thread
        self._claims_cv = threading.Condition(self._lock)

    # ---- snapshot / read ----
    def snapshot(self):
        """Stable copy for iteration. Never iterate the live dict."""
        with self._lock:
            return dict(self._data)

    def get(self, symbol):
        with self._lock:
            return self._data.get(symbol)

    def symbols(self):
        with self._lock:
            return set(self._data)

    def __len__(self):
        with self._lock:
            return len(self._data)

    # ---- mutate in place (never rebind) ----
    def upsert(self, symbol, avg_price, purchase_date):
        with self._lock:
            self._data[symbol] = (avg_price, purchase_date)

    def remove(self, symbol):
        with self._lock:
            self._data.pop(symbol, None)

    def replace_all(self, mapping):
        """Refresh contents WITHOUT rebinding the object other threads hold."""
        with self._lock:
            self._data.clear()
            self._data.update(mapping)

    # ---- per-symbol claim ----
    def claim(self, symbol, timeout=0):
        """Try to take exclusive ownership of a symbol. False if already claimed."""
        with self._claims_cv:
            if symbol in self._claims:
                return False
            self._claims.add(symbol)
            return True

    def release(self, symbol):
        with self._claims_cv:
            self._claims.discard(symbol)
            self._claims_cv.notify_all()


# Single shared instance. Referenced directly by both threads; never reassigned.
position_book = PositionBook()


class ProfitMonitorEngine:
    """
    Peak-following exit. Instead of selling at the first tick above +0.5%, this
    arms at that level and then follows price to its high-water mark, selling
    only once price gives back PEAK_GIVEBACK_PCT from the peak.

    States per symbol:
      watching -> below the arm threshold, do nothing
      armed    -> above arm threshold, tracking peak_price
      exit     -> pulled back from peak, sell now

    There is no holding-period gate: a position can arm and exit the same
    second it was bought.
    """

    def __init__(self):
        self._state = {}          # symbol -> dict(peak_price, armed_at, last_seen, floor_pct)
        self._lock = threading.Lock()

    @staticmethod
    def _arm_threshold_for(atr_pct):
        """ATR-scaled arm threshold, or the flat fallback if ATR% is unavailable."""
        if atr_pct is None or atr_pct <= 0:
            return ARM_PROFIT_PCT
        return max(ARM_PROFIT_PCT, ATR_ARM_MULTIPLIER * atr_pct)

    @staticmethod
    def _giveback_for_peak(peak_gain_pct, arm_pct):
        """
        REVIEW ITEM #9: giveback now scales with how far the position has
        ACTUALLY run (peak_gain_pct), not just with the fixed arm threshold
        from when it first armed. A position that ran to +6% gets a wider
        giveback allowance than one that just barely armed at +1.2%, letting
        a strong trend breathe instead of being cut at the same tight margin
        every time. The arm-based floor (ATR_GIVEBACK_FRACTION x arm_pct)
        still applies as a MINIMUM, so a position that hasn't moved much past
        arming doesn't get an unreasonably wide giveback either.
        """
        arm_based_floor = ATR_GIVEBACK_FRACTION * arm_pct
        peak_based = PEAK_GIVEBACK_FRACTION * peak_gain_pct
        return max(PEAK_GIVEBACK_PCT, arm_based_floor, peak_based)

    def evaluate(self, symbol, entry_price, current_price, atr_pct=None):
        """Returns (should_sell: bool, info: dict) for logging/telemetry."""
        now = time.time()
        if not entry_price or entry_price <= 0 or not current_price or current_price <= 0:
            return False, {'state': 'invalid'}

        arm_pct = self._arm_threshold_for(atr_pct)
        gain = (current_price - entry_price) / entry_price

        with self._lock:
            st = self._state.get(symbol)

            # Not yet armed: wait for +arm_pct (ATR-scaled, or the flat fallback).
            if st is None:
                if gain < arm_pct:
                    return False, {'state': 'watching', 'gain_pct': gain * 100,
                                   'arm_at_pct': arm_pct * 100}
                self._state[symbol] = {'peak_price': current_price,
                                       'armed_at': now, 'last_seen': now,
                                       'floor_pct': HARD_FLOOR_PCT, 'arm_pct': arm_pct}
                return False, {'state': 'armed', 'gain_pct': gain * 100,
                               'peak_price': current_price, 'peak_gain_pct': gain * 100}

            # Already armed: ratchet the peak upward, never down.
            st['last_seen'] = now
            if current_price > st['peak_price']:
                st['peak_price'] = current_price
                return False, {'state': 'new_peak', 'gain_pct': gain * 100,
                               'peak_price': st['peak_price'], 'peak_gain_pct': gain * 100}

            peak = st['peak_price']
            peak_gain = (peak - entry_price) / entry_price
            giveback = (peak - current_price) / peak
            floor_pct = st.get('floor_pct', HARD_FLOOR_PCT)
            # Use the arm_pct RECORDED at arm-time for this position (not a
            # possibly-different current ATR reading), so the floor component
            # of the giveback calc stays stable for the life of this armed run.
            arm_pct_for_giveback = st.get('arm_pct', arm_pct)
            giveback_pct = self._giveback_for_peak(peak_gain, arm_pct_for_giveback)

            info = {'state': 'following', 'gain_pct': gain * 100,
                    'peak_price': peak, 'peak_gain_pct': peak_gain * 100,
                    'giveback_pct': giveback * 100, 'giveback_target_pct': giveback_pct * 100}

            # Pulled back enough from peak -> exit, but never give back the
            # whole move: require the position still be profitably above floor.
            if giveback >= giveback_pct and gain >= floor_pct:
                info['state'] = 'exit'
                return True, info

            # Collapsed below this position's floor after arming: cut it here
            # rather than round-trip a winner into a loser. The floor rises to
            # breakeven once a scale-out stage has fired (raise_floor_to_breakeven).
            if gain < floor_pct:
                info['state'] = 'exit_floor'
                return True, info

            return False, info

    def clear(self, symbol):
        with self._lock:
            self._state.pop(symbol, None)

    def raise_floor_to_breakeven(self, symbol, min_floor_pct=0.0005):
        """
        REVIEW ITEM #5: after a scale-out tranche sells part of the position,
        move the remainder's exit floor up to (near) breakeven so a reversal
        can no longer turn the remaining shares into a loser. Called with the
        state already armed (a scale-out only fires above the arm threshold).
        """
        with self._lock:
            st = self._state.get(symbol)
            if st is not None:
                st['floor_pct'] = max(st.get('floor_pct', HARD_FLOOR_PCT), min_floor_pct)

    def prune(self, live_symbols):
        """Drop state for positions that no longer exist or went stale."""
        now = time.time()
        with self._lock:
            for sym in list(self._state):
                if sym not in live_symbols or (now - self._state[sym]['last_seen']) > MONITOR_STALE_SECS:
                    self._state.pop(sym, None)

    def snapshot(self):
        with self._lock:
            return {s: dict(v) for s, v in self._state.items()}


profit_monitor = ProfitMonitorEngine()

# ---------------- Scaled-exit tracking (review item #5) ----------------
# symbol -> {'original_qty': float, 'stages_fired': set(stage_index)}
# Tracks how much of a position's ORIGINAL size has already been scaled out,
# so stage triggers are evaluated against the position as it was at entry,
# not against whatever qty remains after prior partial sells.
_scale_out_lock = threading.Lock()
_scale_out_state = {}


def _scale_out_get_or_init(symbol, current_qty):
    with _scale_out_lock:
        st = _scale_out_state.get(symbol)
        if st is None:
            st = {'original_qty': current_qty, 'stages_fired': set()}
            _scale_out_state[symbol] = st
        return st


def _scale_out_mark_fired(symbol, stage_index):
    with _scale_out_lock:
        st = _scale_out_state.get(symbol)
        if st is not None:
            st['stages_fired'].add(stage_index)


def _scale_out_clear(symbol):
    with _scale_out_lock:
        _scale_out_state.pop(symbol, None)


def sell_stocks(lock):
    print("Starting sell_stocks function...")
    to_remove = []
    now_str = datetime.now(eastern).strftime("Eastern Time | %I:%M:%S %p | %m-%d-%Y |")
    today_date_str = datetime.now(eastern).date().strftime("%Y-%m-%d")

    # NO holding-period gate. PDT is retired under the 2026 margin rules, so a
    # position may be sold the same second it was bought. purchase_date is now
    # recorded for reporting only.
    profit_monitor.prune(position_book.symbols())

    # BUGFIX: iterate a SNAPSHOT. Previously this walked the live shared dict
    # while buy_stocks mutated it -> "dictionary changed size during iteration".
    for symbol, (bought_price, purchase_date) in position_book.snapshot().items():
        # BUGFIX: per-symbol claim stops buy_stocks and sell_stocks from acting
        # on the same symbol at once (a fill landing mid-exit-decision).
        if not position_book.claim(symbol):
            print(f"{symbol}: busy in another thread this cycle. Skipping.")
            continue
        try:
            current_price = get_current_price(symbol)
            if current_price is None:
                continue

            position = api.get_position(symbol)
            bought_price = float(position.avg_entry_price)
            qty = float(position.qty)

            atr = get_average_true_range(symbol)
            atr_pct = (atr / current_price) if atr and current_price else None
            gain_now = (current_price - bought_price) / bought_price if bought_price else 0.0

            # ---------------- Hard stop-loss (review items #6/#7) ----------------
            # Fires independently of the profit monitor's armed/unarmed state --
            # this is the fix for the biggest gap the review identified: a
            # position that goes straight down from entry (never touching the
            # arm threshold) previously had NOTHING forcing an exit. Checked
            # every cycle, before scaled exits or the profit monitor, so a
            # sharp drop is caught on the very next pass regardless of what
            # those other systems are doing.
            if USE_HARD_STOP_LOSS and atr and atr > 0 and bought_price:
                stop_distance_pct = max(HARD_STOP_ATR_MULTIPLIER * (atr / bought_price),
                                        HARD_STOP_MIN_PCT)
                if gain_now <= -stop_distance_pct:
                    print(f"{symbol}: {RED}HARD STOP{RESET} {gain_now*100:.2f}% <= "
                          f"-{stop_distance_pct*100:.2f}% ({HARD_STOP_ATR_MULTIPLIER:.1f}x "
                          f"ATR, floored at -{HARD_STOP_MIN_PCT*100:.1f}%). Selling full "
                          f"position via escalation chain regardless of profit-monitor state.")
                    logging.warning(f"{now_str} HARD STOP triggered for {symbol}: "
                                    f"{gain_now*100:.2f}% <= -{stop_distance_pct*100:.2f}% "
                                    f"(entry ${bought_price:.2f}, current ${current_price:.2f}, "
                                    f"ATR ${atr:.2f}).")
                    _cancel_existing_sell_orders(symbol, "HARD STOP", now_str)
                    filled_qty, notional, steps = _sell_with_escalation(
                        symbol, qty, current_price, 'market', 'day', "HARD STOP", now_str)
                    if filled_qty > 0:
                        avg_fill_price = notional / filled_qty
                        logging.info(f"{now_str} HARD STOP sold {symbol}: {filled_qty:.4f} sh "
                                    f"@ avg ${avg_fill_price:.2f} via [{', '.join(steps)}].")
                        # profit_monitor/_scale_out state is cleared by the
                        # to_remove consumer loop below, but ONLY once the
                        # position is fully closed -- a partial hard-stop fill
                        # correctly leaves that state in place for the shares
                        # still open, same as any other partial sell.
                        to_remove.append((symbol, filled_qty, avg_fill_price))
                    else:
                        print(f"{symbol}: {RED}hard stop escalation produced no fill{RESET} "
                              f"(see warnings above) -- position remains open, will retry "
                              f"the stop check next cycle.")
                    continue  # done with this symbol for this cycle either way

            # ---------------- Scaled exits (review item #5) ----------------
            # Checked before the peak-following exit: sell a fixed fraction of
            # the ORIGINAL position at each configured gain milestone, moving
            # the profit monitor's floor to breakeven once the first stage
            # fires. The remainder keeps running under the normal monitor.
            #
            # BUGFIX: the order used to be marked "fired" (and the floor
            # raised to breakeven) immediately after submit_order() returned,
            # with no check on what the broker actually did with it. A
            # rejected, delayed, canceled, or partially-filled order would
            # still be treated as a completed stage -- the bot could believe
            # it locked in profit on shares that were never actually sold.
            # Now the order is polled to a terminal status first; the stage is
            # only marked fired for the quantity that ACTUALLY filled, using
            # the real fill price for logging, and the floor is only raised
            # if at least some of the tranche confirmed filled.
            if USE_SCALED_EXITS and SCALE_OUT_STAGES:
                sc_state = _scale_out_get_or_init(symbol, qty)
                for idx, (trigger_pct, frac) in enumerate(SCALE_OUT_STAGES):
                    if idx in sc_state['stages_fired']:
                        continue
                    if gain_now < trigger_pct:
                        break  # stages are in ascending order; none further can fire yet
                    scale_qty = round(sc_state['original_qty'] * frac, 4)
                    scale_qty = min(scale_qty, qty)
                    if scale_qty <= 0:
                        _scale_out_mark_fired(symbol, idx)
                        continue
                    print(f"{symbol}: {GREEN}+{gain_now*100:.2f}%{RESET} hit scale-out stage "
                          f"{idx+1} (+{trigger_pct*100:.1f}%) — selling {scale_qty:.4f} sh "
                          f"({frac*100:.0f}% of original). Awaiting fill confirmation before "
                          f"marking complete.")
                    try:
                        so = api.submit_order(symbol=symbol, qty=str(scale_qty), side='sell',
                                              type='market', time_in_force='day')
                    except Exception as e:
                        print(f"{symbol}: scale-out stage {idx+1} submit failed: {e}")
                        logging.error(f"{symbol}: scale-out stage {idx+1} submit failed: {e}")
                        break

                    terminal, filled_qty, filled_price, status = _poll_order_terminal(
                        so.id, SCALE_OUT_FILL_TIMEOUT_SECS)

                    if not terminal:
                        # Still working after the poll budget -- don't guess.
                        # Leave the stage unmarked so the NEXT cycle re-checks
                        # this same order's outcome rather than assuming
                        # anything about it now.
                        print(f"{symbol}: scale-out stage {idx+1} order {so.id} still "
                              f"{status} after {SCALE_OUT_FILL_TIMEOUT_SECS}s; will "
                              f"re-check next cycle. Not marking the stage complete yet.")
                        logging.info(f"{now_str} Scale-out stage {idx+1} for {symbol}: "
                                    f"order {so.id} not yet terminal (status={status}); "
                                    f"deferring to next cycle.")
                        break

                    if filled_qty <= 0:
                        # Rejected/canceled/expired with nothing filled: the
                        # tranche did not execute at all. Do NOT mark fired and
                        # do NOT raise the floor -- retry this same stage on a
                        # later cycle instead of silently losing the attempt.
                        print(f"{symbol}: scale-out stage {idx+1} order {status} with "
                              f"no fill. Will retry this stage on a later cycle.")
                        logging.warning(f"{now_str} Scale-out stage {idx+1} for {symbol}: "
                                       f"order {so.id} ended {status} with 0 filled qty.")
                        break

                    actual_price = filled_price if filled_price else current_price
                    if filled_qty < scale_qty:
                        print(f"{symbol}: scale-out stage {idx+1} PARTIALLY filled — "
                              f"{filled_qty:.4f} of {scale_qty:.4f} sh @ ${actual_price:.2f} "
                              f"(status={status}). Marking this stage complete for the "
                              f"filled amount only; the unfilled remainder is not resubmitted "
                              f"automatically (rare edge case worth checking manually).")
                        logging.warning(f"{now_str} Scale-out stage {idx+1} for {symbol}: "
                                       f"partial fill {filled_qty:.4f}/{scale_qty:.4f} sh "
                                       f"@ ${actual_price:.2f} (status={status}).")
                    else:
                        print(f"{symbol}: scale-out stage {idx+1} CONFIRMED filled — "
                              f"{filled_qty:.4f} sh @ ${actual_price:.2f}. Moving floor to breakeven.")
                        logging.info(f"{now_str} Scale-out stage {idx+1} for {symbol}: "
                                    f"confirmed filled {filled_qty:.4f} sh @ ${actual_price:.2f}.")

                    # Only reached once we KNOW at least part of the tranche
                    # actually filled -- this is the fix: fired/breakeven are
                    # now consequences of a confirmed fill, not of submission.
                    _scale_out_mark_fired(symbol, idx)
                    profit_monitor.raise_floor_to_breakeven(symbol)
                    with open(csv_filename, mode='a', newline='') as f:
                        csv.DictWriter(f, fieldnames=fieldnames).writerow({
                            'Date': now_str, 'Buy': '', 'Sell': 'Sell (scale-out)',
                            'Quantity': filled_qty, 'Symbol': symbol,
                            'Price Per Share': actual_price,
                        })
                    # Only fire one stage per cycle; re-evaluate qty/gain next pass.
                    break

            # ---------------- Exit decision (remaining shares) ----------------
            if USE_PROFIT_MONITOR:
                should_sell, info = profit_monitor.evaluate(symbol, bought_price, current_price, atr_pct=atr_pct)
                state = info.get('state')
                if state in ('watching',):
                    print(f"{symbol}: {info['gain_pct']:+.2f}% (arms at "
                          f"+{info['arm_at_pct']:.2f}%). Holding.")
                    continue
                if state in ('armed', 'new_peak'):
                    print(f"{symbol}: {GREEN}{info['gain_pct']:+.2f}%{RESET} "
                          f"peak ${info['peak_price']:.2f} — following.")
                    continue
                if state == 'following' and not should_sell:
                    gvt = info.get('giveback_target_pct', PEAK_GIVEBACK_PCT * 100)
                    print(f"{symbol}: {GREEN}{info['gain_pct']:+.2f}%{RESET} "
                          f"peak +{info['peak_gain_pct']:.2f}% "
                          f"(giveback {info['giveback_pct']:.2f}% of "
                          f"{gvt:.2f}%). Following.")
                    continue
                if not should_sell:
                    continue
                if state == 'exit_floor':
                    reason = (f"dropped to {info['gain_pct']:+.2f}% after peaking "
                              f"+{info['peak_gain_pct']:.2f}% — cutting at floor")
                else:
                    reason = (f"peaked +{info['peak_gain_pct']:.2f}%, gave back "
                              f"{info['giveback_pct']:.2f}% — taking {info['gain_pct']:+.2f}%")
            else:
                sell_threshold = bought_price * TAKE_PROFIT_PCT
                if current_price < sell_threshold:
                    print(f"{symbol}: {RED}${current_price:.2f}{RESET} < target ${sell_threshold:.2f}. Holding.")
                    continue
                reason = f"hit +{(TAKE_PROFIT_PCT-1)*100:.2f}% target"

            # BUGFIX: cancel the resting trailing stop BEFORE selling. It reserves
            # shares at the broker, so without this the take-profit could only ever
            # sell the unreserved fraction and the whole-share portion was stuck.
            if not cancel_open_sell_orders(symbol):
                print(f"{symbol}: could not clear resting sell orders. Skipping this cycle.")
                continue

            # Re-read the position after cancellation: qty_available now reflects
            # the freed shares, and the position may have changed size.
            try:
                position = api.get_position(symbol)
            except Exception as e:
                print(f"{symbol}: position gone after cancel ({e}). Skipping.")
                logging.info(f"{symbol}: position no longer exists after cancel: {e}")
                continue

            qty = float(position.qty)
            qty_available = float(getattr(position, 'qty_available', qty) or qty)
            # BUGFIX: sell exactly what the broker says is sellable. Rounding a
            # fractional qty to 4dp could exceed the real position and be rejected.
            sell_qty = min(qty, qty_available)
            if sell_qty <= 0:
                print(f"{symbol}: nothing available to sell. Skipping.")
                continue

            print(f"Selling {sell_qty} sh of {symbol} @ {GREEN}${current_price:.2f}{RESET} "
                  f"(entry ${bought_price:.2f}) — {reason}")
            sell_order = api.submit_order(symbol=symbol, qty=str(sell_qty), side='sell',
                                          type='market', time_in_force='day')
            logging.info(f"{now_str} Submitted sell {sell_qty} sh of {symbol} at ~{current_price:.2f}: {reason}")

            # BUGFIX: the original never confirmed the sell filled -- it deleted the
            # DB row immediately, so a rejected sell silently desynced the DB from
            # the broker and the bot believed it was flat while still holding shares.
            sold_qty = 0.0
            sold_price = current_price
            for _ in range(15):
                try:
                    so = api.get_order(sell_order.id)
                except Exception as e:
                    logging.warning(f"{symbol}: sell poll error ({e}); retrying.")
                    time.sleep(2)
                    continue
                sold_qty = float(so.filled_qty or 0)
                if so.filled_avg_price:
                    sold_price = float(so.filled_avg_price)
                if so.status == 'filled':
                    break
                if so.status in ('canceled', 'expired', 'rejected'):
                    logging.warning(f"{symbol}: sell order {so.status}, filled {sold_qty:.4f}.")
                    break
                time.sleep(2)

            if sold_qty <= 0:
                print(f"{symbol}: sell did not fill. Position retained.")
                logging.warning(f"{now_str} Sell not filled for {symbol}; DB row retained.")
                continue

            print(f"Sold {sold_qty:.4f} sh of {symbol} @ {GREEN}${sold_price:.2f}{RESET}")
            logging.info(f"{now_str} Sold {sold_qty:.4f} sh of {symbol} at {sold_price:.2f}")

            with open(csv_filename, mode='a', newline='') as f:
                csv.DictWriter(f, fieldnames=fieldnames).writerow({
                    'Date': now_str, 'Buy': '', 'Sell': 'Sell',
                    'Quantity': sold_qty, 'Symbol': symbol,
                    'Price Per Share': sold_price,
                })
            to_remove.append((symbol, sold_qty, sold_price))

        except Exception as e:
            print(f"Error processing sell for {symbol}: {e}")
            logging.error(f"Error processing sell for {symbol}: {e}")
        finally:
            # BUGFIX: always release the claim, even on the `continue` paths and
            # on exception, or the symbol is permanently locked out of trading.
            position_book.release(symbol)

    if not to_remove:
        return
    try:
        with lock:
            for symbol, qty, price in to_remove:
                session.add(TradeHistory(symbols=symbol, action='sell',
                                         quantity=qty, price=price, date=today_date_str))
                # BUGFIX: a partial sell used to delete the whole Position row,
                # making the bot forget shares it still owned. Decrement instead,
                # and only remove the row when the position is actually closed.
                row = session.query(Position).filter_by(symbols=symbol).one_or_none()
                if row and (row.quantity - qty) > 1e-6:
                    row.quantity -= qty
                    print(f"{symbol}: partial sell, {row.quantity:.4f} sh still held.")
                else:
                    session.query(Position).filter_by(symbols=symbol).delete()
                    position_book.remove(symbol)
                    # Reset peak tracking so a later re-buy starts a fresh run
                    # rather than inheriting the old position's high-water mark.
                    profit_monitor.clear(symbol)
                    _scale_out_clear(symbol)

                    # REVIEW ITEM #7: fill in the outcome on the most recent
                    # still-open TradeFeatures row for this symbol, so the
                    # entry-feature snapshot can be joined against what
                    # actually happened. Only closes the row once the position
                    # is fully flat, matching how a "trade" is defined here.
                    feat_row = (session.query(TradeFeatures)
                               .filter_by(symbols=symbol, exit_date=None)
                               .order_by(TradeFeatures.id.desc())
                               .first())
                    if feat_row and feat_row.entry_price:
                        feat_row.exit_date = today_date_str
                        feat_row.exit_price = price
                        feat_row.outcome_pct = (price - feat_row.entry_price) / feat_row.entry_price
            session.commit()
    except SQLAlchemyError as e:
        session.rollback()
        print(f"Database error: {e}")
        logging.error(f"Database error: {e}")
        return

    # BUGFIX: refresh_after_sell() used to run INSIDE `with lock`. It makes
    # blocking API calls (list_positions + a paginated order lookup per symbol),
    # holding the mutex for tens of seconds and serializing both threads. It is
    # now called after the lock is released.
    refresh_after_sell()


def refresh_after_sell():
    # BUGFIX: no longer rebinds a global. replace_all() mutates the single shared
    # PositionBook in place, so both threads keep seeing the same object.
    position_book.replace_all(update_symbols_to_sell_from_api())


def load_positions_from_database():
    return {p.symbols: (p.avg_price, p.purchase_date) for p in session.query(Position).all()}


def reconcile_positions_on_startup():
    """
    Alpaca is the single source of truth. The local .db is only a cache.

    BUGFIX: main() previously did `load_positions_from_database()` and then only
    called the API `if not symbols_to_sell_dict` -- i.e. it ONLY synced when the
    DB was empty. A non-empty stale DB was therefore NEVER reconciled, so after a
    restart the bot would:
      - try to sell phantom positions closed while it was down (endless
        "position does not exist" errors), and
      - be blind to positions opened by hand or by another process.

    On startup we now:
      1. Pull live positions from Alpaca.
      2. DELETE any DB row with no matching live position.
      3. Insert/update rows for every live position (correcting drifted qty and
         avg_price, since the broker's numbers are authoritative).
      4. Re-arm the profit monitor so an in-flight winner keeps following its
         peak across the restart instead of dumping at the first tick.

    Raises on API failure: starting up on an unverified DB is more dangerous
    than not starting at all.
    """
    print("\n--- Reconciling local database against Alpaca positions ---")

    try:
        live_positions = api.list_positions()
    except Exception as e:
        # Do NOT silently fall back to the stale DB.
        msg = f"FATAL: cannot reach Alpaca to reconcile positions on startup: {e}"
        print(f"{RED}{msg}{RESET}")
        logging.critical(msg)
        raise

    live = {}
    for p in live_positions:
        try:
            live[p.symbol] = {'qty': float(p.qty), 'avg_price': float(p.avg_entry_price)}
        except (TypeError, ValueError) as e:
            logging.error(f"Skipping malformed position {getattr(p, 'symbol', '?')}: {e}")

    db_rows = {r.symbols: r for r in session.query(Position).all()}

    # --- 1. Drop DB rows Alpaca does not know about ---
    orphans = [s for s in db_rows if s not in live]
    for sym in orphans:
        row = db_rows[sym]
        print(f"  {RED}REMOVED{RESET} {sym}: in local DB ({row.quantity:.4f} sh @ "
              f"${row.avg_price:.2f}) but NOT held at Alpaca — deleting stale row.")
        logging.warning(f"Startup reconcile: deleting stale DB position {sym} "
                        f"(qty={row.quantity}, avg={row.avg_price}); not present at broker.")
        session.delete(row)
        profit_monitor.clear(sym)
        _scale_out_clear(sym)

    # --- 2. Insert/correct rows for live positions ---
    result = {}
    for sym, info in live.items():
        qty, avg = info['qty'], info['avg_price']
        row = db_rows.get(sym)

        if row is None:
            pdate = get_most_recent_purchase_date(sym)
            print(f"  {GREEN}ADDED{RESET}   {sym}: held at Alpaca ({qty:.4f} sh @ "
                  f"${avg:.2f}) but missing locally — inserting.")
            logging.warning(f"Startup reconcile: adding untracked broker position {sym}.")
            session.add(Position(symbols=sym, quantity=qty, avg_price=avg, purchase_date=pdate))
        else:
            pdate = row.purchase_date or get_most_recent_purchase_date(sym)
            drift_qty = abs(row.quantity - qty) > 1e-6
            drift_avg = abs(row.avg_price - avg) > 0.005
            if drift_qty or drift_avg:
                print(f"  {RED}CORRECTED{RESET} {sym}: DB had {row.quantity:.4f} sh @ "
                      f"${row.avg_price:.2f}, broker says {qty:.4f} sh @ ${avg:.2f}.")
                logging.warning(f"Startup reconcile: correcting {sym} to broker values.")
            else:
                print(f"  {GREEN}OK{RESET}      {sym}: {qty:.4f} sh @ ${avg:.2f}")
            row.quantity, row.avg_price, row.purchase_date = qty, avg, pdate

        result[sym] = (avg, pdate)

    try:
        session.commit()
    except SQLAlchemyError as e:
        session.rollback()
        logging.critical(f"Startup reconcile commit failed: {e}")
        raise

    # --- 3. Re-arm the profit monitor for positions already in profit ---
    # Without this, a position that had run to +3% before the restart would lose
    # its peak and exit at the next +0.5% tick, giving back the whole move.
    if USE_PROFIT_MONITOR:
        for sym, (avg, _pdate) in result.items():
            cp = get_current_price(sym)
            if cp is None:
                continue
            gain = (cp - avg) / avg if avg else 0
            if gain >= ARM_PROFIT_PCT:
                # Seed the peak at the current price. The true pre-restart peak is
                # unknowable, so this conservatively restarts the ratchet from here.
                profit_monitor.evaluate(sym, avg, cp)
                print(f"  Re-armed profit monitor for {sym} at {gain*100:+.2f}% "
                      f"(peak reset to current price).")

    kept, removed, added = len(result), len(orphans), len([s for s in live if s not in db_rows])
    summary = f"Reconcile complete: {kept} live position(s), {removed} stale row(s) deleted, {added} added."
    print(f"--- {summary} ---\n")
    logging.info(summary)
    return result


def _run_and_release(fn, *args):
    """
    Thread entry point. scoped_session gives each thread its own Session, which
    must be released when the thread finishes or its DB connection leaks.
    Also stops an unhandled exception in a worker from dying silently.
    """
    try:
        fn(*args)
    except Exception as e:
        print(f"Unhandled error in {fn.__name__}: {e}")
        logging.exception(f"Unhandled error in {fn.__name__}: {e}")
    finally:
        Session.remove()


def main():
    global symbols_to_buy
    print("Starting main trading program...")
    symbols_to_buy = get_symbols_to_buy()

    # Resume auto-adjusted parameters from the last run instead of resetting
    # to coded defaults -- the guardrails (min sample, step cap, bounds) still
    # apply to any further adjustment from here.
    adaptive_params.load_from_db()

    # BUGFIX: was `load_positions_from_database()`, which trusted the stale .db
    # on restart. Alpaca is authoritative; reconcile before touching anything.
    position_book.replace_all(reconcile_positions_on_startup())

    # BUGFIX: main() created a fresh `lock = threading.Lock()` while the
    # module-level buy_sell_lock sat unused, which was confusing and made it easy
    # to reintroduce a second, non-shared mutex. Use the one module-level lock.
    lock = buy_sell_lock
    cycle_count = 0

    while True:
        try:
            stop_if_stock_market_is_closed()
            cycle_count += 1
            now_str = datetime.now(eastern).strftime("Eastern Time | %I:%M:%S %p | %m-%d-%Y |")
            st = get_margin_state()

            print("------------------------------------------------------------------------------------\n")
            print("*****************************************************")
            print("******** Billionaire Buying Strategy Version ********")
            print("*****************************************************")
            print("2026 Edition of the Advanced Stock Market Trading Robot, Version 9")
            print("by https://github.com/CodeProSpecialist")
            print("------------------------------------------------------------------------------------")
            print(f" {now_str} Cash Balance: ${st['cash']:,.2f}")
            print(f" Equity: ${st['equity']:,.2f} | Buying Power: ${st['buying_power']:,.2f} | "
                  f"Effective BP (leverage cap {MAX_LEVERAGE:.1f}x): ${st['effective_bp']:,.2f}")
            print(f" Day-trading BP: ${st['daytrading_buying_power']:,.2f} | Reg-T BP: ${st['regt_buying_power']:,.2f}")
            print(f" Margin health (equity/long_mv): {st['margin_ratio']:.2f} "
                  f"(floor {MAINTENANCE_MARGIN_FLOOR_PCT:.2f}) -> "
                  f"{GREEN + 'OK' + RESET if st['healthy'] else RED + 'BREACHED' + RESET}")
            print(f" Account mode: {ACCOUNT_MODE} | Day trades: "
                  f"{'UNLIMITED (2026 margin rules - PDT retired)' if UNLIMITED_DAY_TRADES else 'limited'}")
            try:
                rinfo = get_market_regime()
                vix_s = f"{rinfo['vix']:.1f}" if rinfo['vix'] is not None else "n/a"
                print(f" Market regime: {rinfo['regime'].upper()} (VIX {vix_s}) | "
                      f"buy threshold: {get_buy_score_threshold(rinfo['regime'])}")
            except Exception as e:
                logging.warning(f"Regime banner failed: {e}")
            print("------------------------------------------------------------------------------------\n")

            symbols_to_buy = get_symbols_to_buy()

            # PERF: one batched yf.download() seeds the daily SMA/RSI/ATR cache
            # for every symbol. Without it each symbol costs 3 separate yfinance
            # requests (48 for 16 symbols); batched it is 1. Cheap no-op when the
            # 30m cache TTLs are still warm.
            prewarm_daily_cache(symbols_to_buy)

            # BUGFIX: was `if not symbols_to_sell_dict:` -- the API resync only ran
            # when the dict was EMPTY, so a populated-but-stale view was never
            # corrected. Resync every cycle, in place, before starting threads.
            position_book.replace_all(update_symbols_to_sell_from_api())

            buy_thread = threading.Thread(target=_run_and_release,
                                          args=(buy_stocks, symbols_to_buy, lock),
                                          name='buy')
            sell_thread = threading.Thread(target=_run_and_release,
                                           args=(sell_stocks, lock),
                                           name='sell')
            buy_thread.start()
            sell_thread.start()
            # BUGFIX: bound the join. Without a timeout, a worker wedged on a
            # hung API call would freeze the main loop forever with no output.
            buy_thread.join(timeout=THREAD_JOIN_TIMEOUT)
            sell_thread.join(timeout=THREAD_JOIN_TIMEOUT)
            for t in (buy_thread, sell_thread):
                if t.is_alive():
                    msg = (f"WARNING: {t.name}_stocks thread still running after "
                           f"{THREAD_JOIN_TIMEOUT}s; continuing without it. It holds no "
                           f"lock indefinitely, but check for a hung API call.")
                    print(f"{RED}{msg}{RESET}")
                    logging.error(msg)

            # Runs its own once-per-day/3:45pm gate internally; safe to call
            # every cycle. Only meaningful during regular market hours, which
            # is exactly when this loop body runs.
            try:
                run_close_profit_sweep()
            except Exception as e:
                logging.error(f"Pre-close profit sweep raised: {e}")

            if PRINT_SYMBOLS_TO_BUY:
                print("\nSymbols to Purchase:\n")
                # BUGFIX: original shadowed the `symbols_to_buy` list with the
                # loop variable, destroying the list after the first pass.
                for sym in symbols_to_buy:
                    cp = get_current_price(sym)
                    if cp is None:
                        continue
                    prev = get_previous_price(sym) or cp
                    print(f"Symbol: {sym} | Current Price: {GREEN if cp > prev else RED}${cp:.2f}{RESET}")
                print("")

            if PRINT_ROBOT_STORED_BUY_AND_SELL_LIST_DATABASE:
                print_database_tables()

            if DEBUG:
                print("\nSymbols to Purchase:\n")
                for sym in symbols_to_buy:
                    cp = get_current_price(sym)
                    lo = get_atr_low_price(sym)
                    if cp is None:
                        continue
                    prev = get_previous_price(sym) or cp
                    lo_s = f"${lo:.2f}" if lo else "n/a"
                    print(f"Symbol: {sym} | Current: {GREEN if cp > prev else RED}${cp:.2f}{RESET} | ATR low: {lo_s}")
                print("\nSymbols to Sell:\n")
                for sym in sorted(position_book.symbols()):
                    cp = get_current_price(sym)
                    hi = get_atr_high_price(sym)
                    if cp is None:
                        continue
                    prev = get_previous_price(sym) or cp
                    hi_s = f"${hi:.2f}" if hi else "n/a"
                    print(f"Symbol: {sym} | Current: {GREEN if cp > prev else RED}${cp:.2f}{RESET} | ATR high: {hi_s}")
                print("")

            # REVIEW ITEM #7: informational diagnostics -- prints findings,
            # does not touch live parameters. Separate from the auto-adjuster.
            if cycle_count % ANALYZE_TRADE_HISTORY_EVERY_N_CYCLES == 0:
                try:
                    analyze_trade_history()
                except Exception as e:
                    logging.warning(f"Trade history analysis failed: {e}")

            # REVIEW ITEM #10 (auto-applying, per your instruction): bounded,
            # point-based parameter adjustment. See AdaptiveParams for the
            # guardrails (min sample size, max step size, hard bounds, full
            # audit log) that keep "auto-applies" from meaning "unbounded".
            if cycle_count % ADAPT_EVERY_N_CYCLES == 0:
                try:
                    run_adaptive_parameter_pass()
                except Exception as e:
                    logging.warning(f"Adaptive parameter pass failed: {e}")

            # ML brain: unified scheduled training entry point.
            # Handles internally:
            #   - First-run bootstrap: 2,500-example pretrain if no model exists yet
            #   - Daily 17:00 ET window: 15,000-example historical pretrain (until
            #     cumulative pretraining hits the 20,000 lifetime cap)
            #   - Post-cap daily maintenance: fine-tune on the last day's live
            #     win/loss outcomes only
            # Once-per-window gating + lifetime cap tracking are handled by the
            # scheduling code itself. Cheap to call every cycle -- most calls are
            # a small file check + a JSON read + return None.
            if USE_ML_BRAIN_ADJUSTMENT and _ml_brain_is_available():
                try:
                    ml_status = maybe_run_scheduled_ml_training()
                    if ml_status:
                        print(f"ML brain training: {ml_status}")
                        logging.info(f"ML brain training: {ml_status}")
                except Exception as e:
                    logging.warning(f"ML brain scheduled training check failed: {e}")

            print("Waiting 1 minute before checking price data again........")
            time.sleep(60)

        except Exception as e:
            logging.error(f"Error encountered: {e}")
            print(f"Error encountered in main loop: {e}")
            time.sleep(120)


if __name__ == '__main__':
    try:
        print("Initializing trading bot...")
        main()
    except KeyboardInterrupt:
        print("\nShutting down.")
    except Exception as e:
        logging.error(f"Error encountered: {e}")
        print(f"Critical error: {e}")
    finally:
        Session.remove()
