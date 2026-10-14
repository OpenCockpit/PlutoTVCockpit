# PlutoTVCockpit (PTV)
Open-Enigma2 plugin for Live-TV and VoD streams

## Live-TV

![Screenshot](pluto1.jpg)

## Video-On-Demand

![Screenshot](pluto2.jpg)

## Configuration

![Screenshot](pluto3.jpg)

## Features
- Playback of Pluto TV live channels and VoD content by country
- Generation of E2 bouquets including EPG
- Optional stream proxy (setting *Stream proxy*, on by default): the channels are played and recorded through StreamProxyCockpit (the bouquet holds a lazy `pluto://<channel id>` address per channel). Off, the bouquet holds the addresses of the selected source (setting *Live TV mode*, only used then: Stitcher, JMP2 or i.mjh.nz; Stitcher by default). The bouquet of each setting is kept (`.tv.proxy`, `.tv.noproxy`) and `.tv` always holds the one of the current setting: changing it switches between them, a bouquet that does not exist yet or is older than the update interval (5 hours) is generated
- Download of channel picons
- Record channels - instant and timer recording

## Disclaimer
The project author is not responsible for how this software is used by others. It is not intended to be used for accessing or distributing copyrighted materials without authorization.
Users are solely responsible for determining the legality of their actions.

This repository has no control over the streams, links, or the legality of the content provided by the different hosts (including all mirror sites). It is the end user's responsibility to ensure the legal use of these streams, and we strongly recommend verifying that the content complies with all applicable laws, including copyright laws and regulations of your country's jurisdiction before use.

## Limitations
- Tested on OpenViX and OpenATV with DM900.

## Links
- Installation: https://OpenCockpit.github.io/PlutoTVCockpit
- User Guide: https://github.com/OpenCockpit/PlutoTVCockpit/wiki
