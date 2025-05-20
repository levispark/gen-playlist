import re
import certifi
import os
import requests


def runServers():
    with open("playlist1.m3u8", "w") as file:
        file.write("#EXTM3U\n")
    for i in range(len(lis)):
        print(f"{i+1}.{lis[i]}")
        server1(i + 1, lis[i])
        
    with open("playlist2.m3u8", "w") as file:
        file.write("#EXTM3U\n")
    for i in range(len(hashCode)):
        print(f"{i+1}.{channels[i]}")
        server2(hashCode[i], channels[i])

    with open("playlist3.m3u8", "w") as file:
        file.write("#EXTM3U\n")
    for i in range(len(hashcode_3)):
        print(f"{i+1}.{channels_3[i]}")
        server3(hashcode_3[i], channels_3[i])


def server1(i, name):
    print("Running Server 1")
    url = f"https://adult-tv-channels.com/tv/{name}.php"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://adult-tv-channels.com",
        "X-Requested-With": "XMLHttpRequest",
    }

    response = requests.get(url, headers=headers, verify=certifi.where())

    # Use regex to extract the source URL
    match = re.search(r'file:\s*"([^"]+playlist\.m3u8[^"]*)"', response.text)
    if match:
        stream_url = match.group(1)
        # print(stream_url)
        with open("playlist1.m3u8", "a") as file:
            file.write(f"#EXTINF:-1,{name}\n")
            file.write(f"{stream_url}\n")

    else:
        print("No URL found.")


def server2(hash, name):
    print("Running Server 2")
    res = requests.post(
        f"https://adult-tv-channels.click/C1Ep6maUdBIeKDQypo7a/{hash}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    data = res.json()
    token = data["fileUrl"]

    stream_url = f"https://moonlight.wideiptv.top/{name}/index.fmp4.m3u8?token={token}"
    with open("playlist2.m3u8", "a") as file:
        file.write(f"#EXTINF:-1,{name}\n")
        file.write(f"{stream_url}\n")
    # print(stream_url)


def server3(hash, name):
    print("Running Server 3")
    url = f"https://fuckflix.click/8RLxsc2AW1q8pvyvjqIQ"
    res = requests.post(
        f"{url}/{hash}", headers={"Content-Type": "application/x-www-form-urlencoded"}
    )

    data = res.json()
    token = data["fileUrl"]

    stream_url = f"https://moonlight.wideiptv.top/{name}/index.fmp4.m3u8?token={token}"
    with open("playlist3.m3u8", "a") as file:
        file.write(f"#EXTINF:-1,{name}\n")
        file.write(f"{stream_url}\n")


#   print(stream_url)


# For Server 1
print("Available Channels\nSome links might not works!!!")
lis = [
    "brazzerstv",
    "hustlerhd",
    "hustlertv",
    "penthouse",
    "redlight",
    "penthousepassion",
    "vivid",
    "dorcel",
    "superone",
    "oxax",
    "passie",
    "eroxxx",
    "playboy",
    "pinko",
    "extasy",
    "penthousereality",
    "kinoxxx",
    "pinkerotic",
    "pinkerotic7",
    "pinkerotic8",
    "evilangel",
    "private",
    "beate",
    "meiden",
    "centoxcento",
    "barelylegal",
    "venus",
    "freextv",
    "erox",
    "passion",
    "satisfaction",
    "jasmin",
    "fap",
    "olala",
    "miamitv",
]

# for Server 2
hashCode = [
    "Sdw0p0xE3E",
    "yoni9C8jfd",
    "ZS40W182Zq",
    "czS16artgz",
    "xBFRYv6yXh",
    "hghdvp9Z03",
    "ByYpxFkJZe",
    "5LvPjA7oms",
    "HdcCGPssEy",
    "sI8DBZkklJ",
    "sSEWMS7slF",
    "dRTbLz32p7",
    "Sd6GJ5uMmj",
    "IDLur5k1x2",
    "4FVedsyYlB",
    "S8XdeQ0R1t",
    "svpUwVLRR8",
    "A2PZR5jdH8",
    "3uGUuSP7HX",
    "oEd93JisZ3",
    "E3WyHBCn6j",
    "5QeEhtMv0v",
    "ZQgSJJmzAx",
    "JTzDFcBdgp",
    "58Nyzda2hb",
    "ZvBCE7cpgP",
    "V2D4lPbasF",
    "t6VXUhiBYF",
    "JiA1DWNWJc",
]


channels = [
    "ExxxoticaTV",
    "LeoTV",
    "LeoGoldTV",
    "EvilAngel",
    "VIXEN",
    "Extasy4K",
    "PinkoClubTV",
    "BrazzersTVEU",
    "HustlerHD",
    "RedlightHD",
    "SecretCircleTV",
    "PenthouseGold",
    "Television-X",
    "Private",
    "HOT-HD",
    "BODYSEX",
    "DorcelTV",
    "TransAngels",
    "SuperONE",
    "SextremeTV",
    "SeXation",
    "PassionXXX",
    "HustlerTV",
    "EroX-XxX",
    "EroLuxeShemales",
    "DesireTV",
    "CentoXCento",
    "Barely-Legal-TV",
    "Venus",
]

hashcode_3 = [
    "CudzGm9xm6",
    "T3PIyktDDU",
    "9itOC3AHqJ",
    "OWMDBFfu89",
    "QOOfbBqT4v",
    "2x7HptDKuX",
    "esdMCy0VGM",
    "6s6dIMWGXi",
    "Sdw0p0xE3E",
    "ZS40W182Zq",
    "yoni9C8jfd",
    "czS16artgz",
    "hghdvp9Z03",
    "xBFRYv6yXh",
    "E3WyHBCn6j",
    "HdcCGPssEy",
    "ByYpxFkJZe",
    "Sd6GJ5uMmj",
    "t6VXUhiBYF",
    "58Nyzda2hb",
    "sSEWMS7slF",
    "s4URaZHdvZ",
    "sI8DBZkklJ",
    "YC81XHWeHu",
    "v3UeIcgWXa",
    "dRTbLz32p7",
    "IDLur5k1x2",
    "JAZlXsiLni",
    "R4ol8r2lki",
    "kpTVK5NF1w",
    "m6Elk7hY4x",
    "S8XdeQ0R1t",
    "4FVedsyYlB",
    "svpUwVLRR8",
    "A2PZR5jdH8",
    "3uGUuSP7HX",
    "oEd93JisZ3",
    "5QeEhtMv0v",
    "ZQgSJJmzAx",
    "JTzDFcBdgp",
    "ZvBCE7cpgP",
    "V2D4lPbasF",
    "JiA1DWNWJc",
    "jK2r6H1Dlj",
]
channels_3 = [
    "Tiny4k1",
    "Tiny4k2",
    "Tiny4k3",
    "PenthouseBLACK",
    "Penthouse",
    "NuartTV",
    "Mofos",
    "cum4k",
    "ExxxoticaTV",
    "LeoGoldTV",
    "LeoTV",
    "EvilAngel",
    "Extasy4K",
    "VIXEN",
    "SeXation",
    "HustlerHD",
    "PinkoClubTV",
    "Television-X",
    "Barely-Legal-TV",
    "EroLuxeShemales",
    "SecretCircleTV",
    "Beate-Uhse",
    "RedlightHD",
    "DorcelTVAfrica",
    "PlayboyTV",
    "PenthouseGold",
    "Private",
    "HOTMan",
    "SexyHOT",
    "TransErotica",
    "HOTXXL",
    "BODYSEX",
    "HOT-HD",
    "DorcelTV",
    "TransAngels",
    "SuperONE",
    "SextremeTV",
    "PassionXXX",
    "HustlerTV",
    "EroX-XxX",
    "DesireTV",
    "CentoXCento",
    "Venus",
    "XXL",
]



runServers() #Runs the function to start the servers!
