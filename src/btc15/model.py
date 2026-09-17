from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass


def clamp(value: float, low: float = .005, high: float = .995) -> float:
    return min(high,max(low,value))


def normal_cdf(z: float) -> float:
    return .5*(1+math.erf(z/math.sqrt(2)))


def logit(p: float) -> float:
    p = clamp(p)
    return math.log(p/(1-p))


def logistic(value: float) -> float:
    if value >= 0:
        e = math.exp(-min(value,700))
        return 1/(1+e)
    e = math.exp(max(value,-700))
    return e/(1+e)


def settlement_probability(spot: float, target: float, seconds_left: float,
                           log_vol_per_sqrt_second: float,
                           observed_average: float | None = None,
                           observed_count: int = 0, total_observations: int = 60,
                           drift_per_second: float = 0.0) -> tuple[float,float,float]:
    """Approximate P(final 60-second BRTI average >= target).

    Future BRTI is modeled as a drift-shrunk arithmetic Brownian path. The
    covariance of every remaining one-second observation is included, along
    with uncertainty before the final-minute observation window starts.
    """
    if min(spot,target) <= 0 or total_observations <= 0:
        return .5,target,0.0
    count = min(max(int(observed_count),0),total_observations)
    remaining = total_observations-count
    known_sum = (observed_average if observed_average is not None else spot)*count
    delay = max(0.0,seconds_left-remaining)
    if remaining == 0:
        final_average = known_sum/total_observations
        return (.995 if final_average >= target else .005),final_average,0.0
    # Drift is deliberately capped and shrunk; extrapolating tick momentum is unstable.
    sigma_log = max(log_vol_per_sqrt_second,1e-7)
    drift = max(-2*sigma_log,min(2*sigma_log,drift_per_second))*.25
    average_horizon = delay+(remaining+1)/2
    expected_future = spot*math.exp(drift*average_horizon)
    expected_average = (known_sum+remaining*expected_future)/total_observations
    sum_min = remaining*(remaining+1)*(2*remaining+1)/6
    covariance_sum = remaining*remaining*delay+sum_min
    standard_deviation = spot*sigma_log*math.sqrt(covariance_sum)/total_observations
    if standard_deviation <= 1e-9:
        probability = .995 if expected_average >= target else .005
    else:
        probability = clamp(normal_cdf((expected_average-target)/standard_deviation))
    return probability,expected_average,standard_deviation


@dataclass
class ModelEstimate:
    p_yes: float
    p_terminal: float
    p_settlement: float
    p_trend: float
    p_market: float | None
    trend_strength: float
    horizon_agreement: float
    uncertainty: float
    confidence: float
    disagreement: float
    expected_settlement: float
    settlement_std: float
    regime: str
    source: str

    def dict(self) -> dict:
        return asdict(self)


class MicrostructureState:
    """Exponentially decayed observable book and aggressive-trade pressure."""
    def __init__(self,half_life: float = 8.0) -> None:
        self.half_life = half_life
        self.book_flow = 0.0
        self.trade_flow = 0.0
        self.last_ts = 0.0
        self.events = 0

    def _decay(self,now: float) -> None:
        if self.last_ts:
            weight = math.exp(-math.log(2)*max(0,now-self.last_ts)/self.half_life)
            self.book_flow *= weight
            self.trade_flow *= weight
        self.last_ts = now

    def reset(self,now: float | None = None) -> None:
        self.book_flow=self.trade_flow=0.0
        self.events=0
        self.last_ts=now or time.time()

    def on_delta(self,side: str,delta: float,now: float) -> None:
        self._decay(now)
        # Adding YES bids or removing YES asks is bullish in unified YES-price space.
        direction = 1 if side == "yes" else -1
        impulse = direction*delta/(abs(delta)+5)
        self.book_flow=max(-1,min(1,self.book_flow+impulse))
        self.events += 1

    def on_trade(self,taker_side: str | None,count: float,now: float) -> None:
        self._decay(now)
        direction = 1 if taker_side == "yes" else (-1 if taker_side == "no" else 0)
        impulse = direction*count/(abs(count)+5)
        self.trade_flow=max(-1,min(1,self.trade_flow+impulse))
        self.events += 1


class PlattCalibrator:
    """Small, auditable calibrator; accepted only after out-of-sample improvement."""
    def __init__(self,min_samples: int = 200) -> None:
        self.minimum_samples = min_samples
        self.a,self.b = 1.0,0.0
        self.samples = 0
        self.validation_brier: float | None = None
        self.baseline_brier: float | None = None
        self.active = False

    def fit(self, samples: list[tuple[float,int]]) -> None:
        self.samples = len(samples)
        self.active = False
        if len(samples) < self.minimum_samples:
            return
        cut = max(self.minimum_samples//2,int(len(samples)*.8))
        train,validation = samples[:cut],samples[cut:]
        a,b = 1.0,0.0
        for _ in range(800):
            da=db=0.0
            for probability,outcome in train:
                x = logit(probability)
                error = logistic(a*x+b)-outcome
                da += error*x
                db += error
            scale = max(1,len(train))
            a -= .03*(da/scale+.002*(a-1))
            b -= .03*db/scale
            a = min(3,max(.2,a))
            b = min(2,max(-2,b))
        baseline = sum((p-y)**2 for p,y in validation)/max(1,len(validation))
        calibrated = sum((logistic(a*logit(p)+b)-y)**2 for p,y in validation)/max(1,len(validation))
        self.baseline_brier,self.validation_brier = baseline,calibrated
        if validation and calibrated < baseline:
            self.a,self.b,self.active = a,b,True

    def predict(self, probability: float) -> float:
        return clamp(logistic(self.a*logit(probability)+self.b)) if self.active else probability

    def status(self) -> dict:
        return {"active":self.active,"samples":self.samples,"minimum_samples":self.minimum_samples,
                "validation_brier":self.validation_brier,"baseline_brier":self.baseline_brier,
                "slope":self.a,"intercept":self.b}


def classify_regime(momentum_5s: float, momentum_30s: float, volatility: float,
                    crossings: int, flow: float) -> str:
    aligned = momentum_5s*momentum_30s > 0
    if volatility > .001:
        return "jump"
    if crossings >= 4 and abs(momentum_30s) < .001:
        return "chop"
    if aligned and abs(momentum_30s) > max(.00025,2*volatility):
        return "trend-up" if momentum_30s > 0 else "trend-down"
    if abs(flow) > .35:
        return "flow-up" if flow > 0 else "flow-down"
    return "balanced"


def estimate_probability(*, spot: float, target: float, seconds_left: float,
                         volatility: float, terminal_probability: float,
                         settlement_average: float | None, observation_count: int,
                         momentum_5s: float, momentum_30s: float, momentum_60s: float,
                         weighted_imbalance: float, book_flow: float, trade_flow: float,
                         crossings: int,
                         market_bid: float | None, market_ask: float | None,
                         history_seconds: float, calibrator: PlattCalibrator) -> ModelEstimate:
    p_settlement,expected,std = settlement_probability(
        spot,target,seconds_left,volatility,settlement_average,observation_count,
        drift_per_second=0.0)
    def normalized_move(change: float,horizon: float) -> float:
        scale=max(1e-7,volatility*math.sqrt(horizon))
        return max(-2.0,min(2.0,change/scale))
    strengths=(normalized_move(momentum_5s,5),normalized_move(momentum_30s,30),
               normalized_move(momentum_60s,60))
    trend_strength=.15*strengths[0]+.35*strengths[1]+.50*strengths[2]
    # Only a quarter of the observed multi-horizon drift reaches the settlement
    # projection. Microstructure research shows that extrapolation decays quickly.
    observed_drift=.35*(momentum_30s/30)+.65*(momentum_60s/60)
    p_trend,_,_=settlement_probability(
        spot,target,seconds_left,volatility,settlement_average,observation_count,
        drift_per_second=observed_drift)
    directional=[1 if value>.12 else (-1 if value<-.12 else 0) for value in strengths]
    up=sum(value>0 for value in directional)
    down=sum(value<0 for value in directional)
    horizon_agreement=max(up,down)/3
    flow = max(-1,min(1,.45*weighted_imbalance+.35*book_flow+.20*trade_flow))
    regime = classify_regime(momentum_5s,momentum_30s,volatility,crossings,flow)
    # Settlement mechanics dominate. Trend is a distinct forecast; order flow only
    # times an entry because its directional information usually lasts seconds.
    model_probability = logistic(
        .68*logit(p_settlement)+.17*logit(p_trend)+.15*logit(terminal_probability)+.04*flow)
    p_market = ((market_bid+market_ask)/2
                if market_bid is not None and market_ask is not None else None)
    disagreement=max(abs(p_settlement-terminal_probability),abs(p_settlement-p_trend))
    data_confidence = min(1,max(0,history_seconds)/60)
    agreement_confidence = max(.15,1-min(1,disagreement/.30))
    confidence = max(.10,min(.95,data_confidence*agreement_confidence))
    # The market is a useful prior but never the label. Shrink more when our data is weak.
    market_weight = .10+.25*(1-confidence) if p_market is not None else 0
    ensemble = (1-market_weight)*model_probability+market_weight*(p_market or 0)
    ensemble = calibrator.predict(clamp(ensemble))
    uncertainty = min(.5,max(.01,(1-confidence)*.20+disagreement*.5))
    source = "settlement-ensemble+validated-platt" if calibrator.active else "settlement-ensemble"
    return ModelEstimate(clamp(ensemble),terminal_probability,p_settlement,p_trend,p_market,
                         trend_strength,horizon_agreement,uncertainty,confidence,disagreement,
                         expected,std,regime,source)
