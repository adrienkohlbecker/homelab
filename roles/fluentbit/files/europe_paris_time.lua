-- Convert offset-less wall-clock timestamps emitted by containers configured
-- with TZ=Europe/Paris into Fluent Bit UTC event timestamps. Call this only
-- from service-owned filters whose source container declares that timezone.

local MONTHS = {
    Apr = 4,
    Aug = 8,
    Dec = 12,
    Feb = 2,
    Jan = 1,
    Jul = 7,
    Jun = 6,
    Mar = 3,
    May = 5,
    Nov = 11,
    Oct = 10,
    Sep = 9,
}

local function is_leap_year(year)
    return year % 4 == 0 and (year % 100 ~= 0 or year % 400 == 0)
end

local function days_in_month(year, month)
    local lengths = { 31, is_leap_year(year) and 29 or 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31 }
    return lengths[month]
end

-- Gregorian civil date to days since 1970-01-01. The arithmetic is timezone
-- independent, unlike os.time(), whose result follows the process-wide TZ.
local function days_from_civil(year, month, day)
    if month <= 2 then
        year = year - 1
    end
    local era = math.floor(year / 400)
    local year_of_era = year - era * 400
    local shifted_month = month > 2 and month - 3 or month + 9
    local day_of_year = math.floor((153 * shifted_month + 2) / 5) + day - 1
    local day_of_era =
        year_of_era * 365 + math.floor(year_of_era / 4) - math.floor(year_of_era / 100) + day_of_year
    return era * 146097 + day_of_era - 719468
end

local function last_sunday(year, month)
    local last_day = days_in_month(year, month)
    -- 1970-01-01 was Thursday; Sunday is weekday zero.
    local weekday = (days_from_civil(year, month, last_day) + 4) % 7
    return last_day - weekday
end

local function utc_offset(year, month, day, hour)
    if month < 3 or month > 10 then
        return 3600
    end
    if month > 3 and month < 10 then
        return 7200
    end

    local transition_day = last_sunday(year, month)
    if day < transition_day then
        return month == 3 and 3600 or 7200
    end
    if day > transition_day then
        return month == 3 and 7200 or 3600
    end

    if month == 3 then
        -- Europe/Paris skips from 01:59:59 UTC+1 to 03:00:00 UTC+2.
        return hour >= 3 and 7200 or 3600
    end
    -- The repeated 02:xx hour is inherently ambiguous in an offset-less log.
    -- Prefer standard time, matching the post-transition side of the stream.
    return hour >= 2 and 3600 or 7200
end

local function valid_parts(year, month, day, hour, minute, second, nanosecond)
    return year >= 1970
        and month >= 1
        and month <= 12
        and day >= 1
        and day <= days_in_month(year, month)
        and hour >= 0
        and hour <= 23
        and minute >= 0
        and minute <= 59
        and second >= 0
        and second <= 60
        and nanosecond >= 0
        and nanosecond < 1000000000
end

local function parts(year, month, day, hour, minute, second, nanosecond)
    year = tonumber(year)
    month = tonumber(month)
    day = tonumber(day)
    hour = tonumber(hour)
    minute = tonumber(minute)
    second = tonumber(second)
    nanosecond = tonumber(nanosecond) or 0
    if year == nil or month == nil or day == nil or hour == nil or minute == nil or second == nil then
        return nil
    end
    if not valid_parts(year, month, day, hour, minute, second, nanosecond) then
        return nil
    end
    return year, month, day, hour, minute, second, nanosecond
end

local function parse_iso(value)
    return parts(value:match("^(%d%d%d%d)%-(%d%d)%-(%d%d) (%d%d):(%d%d):(%d%d)$"))
end

local function parse_plex(value)
    local month_name, day, year, hour, minute, second, milliseconds =
        value:match("^(%a%a%a) (%d%d), (%d%d%d%d) (%d%d):(%d%d):(%d%d)%.(%d%d%d)$")
    if month_name == nil or milliseconds == nil then
        return nil
    end
    return parts(year, MONTHS[month_name], day, hour, minute, second, tonumber(milliseconds) * 1000000)
end

local SOURCES = {
    nzbtomedia_timestamp = parse_iso,
    plex_timestamp = parse_plex,
    tautulli_websocket_timestamp = parse_iso,
}

function from_europe_paris(tag, timestamp, record)
    for field, parse in pairs(SOURCES) do
        local value = record[field]
        if type(value) == "string" then
            local year, month, day, hour, minute, second, nanosecond = parse(value)
            if year == nil then
                record["parse_error"] = "europe_paris_time"
                record["_level"] = "warn"
                return 2, timestamp, record
            end

            local seconds = days_from_civil(year, month, day) * 86400
                + hour * 3600
                + minute * 60
                + second
                - utc_offset(year, month, day, hour)
            return 1, { sec = seconds, nsec = nanosecond }, record
        end
    end
    return 0, timestamp, record
end
