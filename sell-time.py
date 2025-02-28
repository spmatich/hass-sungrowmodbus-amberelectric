#!/usr/bin/env python
# coding: utf-8

from numpy import trapz, argmax
import pandas as pd
import math
from homeassistant.util import dt as dt_util


# below we take a copy of a dataframe and do an assignment on it                                                
# this is deliberate, we dont want this to propate to the original        
# so disable the warning                                                            
pd.options.mode.chained_assignment = None


def sell_price_above_floor(sensors, reserve_pct, est_earn, floor_price):
    # if any of the battery sensors are unavailable calculation of the
    # estimated average sell price can give a divide by zero. So we calculate the
    # inverse_floor_price instead, and reverse the comparison.
    inverse_floor_price = 200 # ie $0.005 in case a zero/negative value is passed 
    if floor_price > 0.0:
        inverse_floor_price = 1 / floor_price
    inverse_est_avg_sell_price = (sensors['battery_capacity'] / 1000) * \
                                    (sensors['battery_level'] - reserve_pct)/100 * \
                                     sensors['battery_state_of_health'] / est_earn
    return(inverse_floor_price > inverse_est_avg_sell_price > 0 )


def get_sell_start_sensors():
    forecasts = state.getattr('sensor.amber_feed_in_forecast')
    general_price = state.getattr('sensor.amber_feed_in_price')
    next_sunrise = pd.Timestamp(dt_util.parse_datetime(state.get('sensor.sun_next_rising')))
    
    battery_level = battery_capacity = battery_state_of_health = 0.0
    if state.get('sensor.battery_level') != "unavailable":
        battery_level = float(state.get('sensor.battery_level'))
    if state.get('sensor.battery_capacity') != "unavailable":
        battery_capacity = float(state.get('sensor.battery_capacity')) * 1000 # Watt-Hours
    if state.get('sensor.battery_state_of_health') != "unavailable":
        battery_state_of_health = float(state.get('sensor.battery_state_of_health')) / 100
    return {"forecasts": forecasts['forecasts'],
            "general_price": general_price,
            "next_sunrise": next_sunrise.timestamp(),
            "battery_level": battery_level,
            "battery_capacity": battery_capacity,
            "battery_state_of_health": battery_state_of_health
           }

    
# this function will read the forecast prices from the Amber sensor (NEM RRP) and 
# pick a time to start discharging the battery that maximises return value
# it also reads the battery sensors in order to calculate battery discharge duration
# the discharge duration determines how many forecast intervals to include for maximum return
# if the estimated earnings from battery discharge corresponds to an average price less
# than the floor_price parameter, dont sell 
@service(supports_response="only")
def find_sell_start(discharge_pwr=10000, reserve_pct=30, floor_price=0.03):

    sensors = get_sell_start_sensors()
    local_tz = dt_util.get_default_time_zone()
 
    rval = {"sell_start": "1970-01-01 00:00:00+11:00",
            "from_now_index": "-1",
            "from_now_delta": "0",
            "est_earn": "0",
            "duration": "0",
            "battery_level": '{}%'.format(sensors['battery_level']) }

    discharge_duration = 3600 * (sensors['battery_state_of_health'] * sensors['battery_capacity'] * \
                                 (sensors['battery_level'] - reserve_pct)/100)/discharge_pwr
    # If discharge duration is negative, battery has not yet reached reserve level.
    # So there is no point calculating the best sell time 
    if discharge_duration <= 0:
        rval['duration'] = int(discharge_duration) 
    else:
        d = []
        d.insert(0,{'Price': sensors['general_price']['per_kwh'],
                    'Raw_time': sensors['general_price']['start_time']})
        for i in sensors['forecasts']:
            d.append({'Price': i['per_kwh'],
                  'Raw_time': i['start_time']})
    
        price_data = pd.DataFrame(d)
        price_data['Timestamp'] = pd.to_datetime(price_data['Raw_time']).dt.tz_convert(local_tz)
        price_data.index = price_data['Timestamp']
        # cant calculate the first tdelta as there is no previous interval, assumes all tdelta have same value 
        price_data['tdelta'] = (price_data['Timestamp'] - price_data['Timestamp'].shift()).bfill()
        price_data['sell_amt'] = 0.0
        
        # a subdf is a dataframe for which the number of elements is determined by
        # the number of tdelta intervals in the discharge time
        # subdf_length is rounded up to include a partial last tdelta interval
        subdf_length = math.ceil(discharge_duration / price_data['tdelta'].iloc[1].total_seconds())
        # calculate the fraction = remainder / tdelta, this is used to scale the price value of the last element
        remainder_fraction = (discharge_duration % price_data['tdelta'].iloc[1].total_seconds()) / \
                                price_data['tdelta'].iloc[1].total_seconds()
        sell_start = price_data['Timestamp'].iloc[0]
        est_earn = 0.0
        for i in range(price_data.shape[0]):
            # Sunrise is the represents the end/start of day for selling power from storage
            # after sunrise we start getting power on the panels and can charge the battery again.
            # There is an implicit darkness sell imperative when looking for the best sell window
            # for the battery, therefore we reset our sell window search after sunrise each day.
            if price_data['Timestamp'].iloc[i].timestamp() <= sensors['next_sunrise']: 
                price_subdf = price_data.iloc[i:i+subdf_length].copy()
                price_subdf['Price'].iloc[-1] *= remainder_fraction
                price_integral = 0
                if subdf_length <= 1:
                    # definite integration does not work with one data point
                    # and in that case trapz returns zero for a single value array
                    # this might happen if the battery discharge would complete in less than one tdelta
                    # so just pick the current interval price in that case
                    price_integral = price_subdf['Price'].iloc[0] * 0.5 * discharge_duration / 3600
                # to choose the highest value ensure comparison of the same number intervals (ie shape)
                # for each window. Also 5400s is a cutoff for exports in the morning, ie the *end* of
                # the sell window can be up to 1.5H after the next sunrise if sell window starts before then
                elif price_subdf.shape[0] == subdf_length and \
                     price_subdf['Timestamp'].iloc[-1].timestamp() <= sensors['next_sunrise'] + 5400:
                    price_integral = trapz(price_subdf['Price'])
                price_data['sell_amt'].iloc[i] = ( price_integral * discharge_pwr * 3.6 / discharge_duration ) \
                    if price_integral > 0 else 0.0

        sell_index = argmax(price_data['sell_amt'], axis=0)
        if sell_price_above_floor(sensors, reserve_pct, price_data['sell_amt'].iloc[sell_index], floor_price):
            rval['sell_start'] = '{}'.format(price_data['Timestamp'].iloc[sell_index])
        rval['from_now_delta'] = '{:.3f}'.format(price_data['sell_amt'].iloc[sell_index] - price_data['sell_amt'].iloc[0])
        rval['from_now_index'] = '{}'.format(sell_index)
        rval['est_earn'] = '{:.2f}'.format(price_data['sell_amt'].iloc[sell_index])
        rval['duration'] = '{:d}'.format(int(discharge_duration))
    return rval

