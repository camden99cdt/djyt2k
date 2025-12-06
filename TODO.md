bug: when seek reaches end of track, play/pause button still reads 'pause' and now playing position stays at the end of the track rather than resetting to 0:00

feature: add a mute button - clicking on the volume % mutes the track, clicking again unmutes

change: change the pitch shifter to work in semitones, rather than half semitones. change the range of the sider to -6 to +6.

feature: display the demucs separation progress that's normally displayed in the stdout in the log

feature: tabs - change the area above the playback visualiser to a tabbed interface
    - tab 1: "YouTube" contains every that used to be above the playback visualizer besides the thumbnail - YouTube URL entry, Donwload Button, Skip separation, log, etc
    - tab 2: "Playback": displays the thumbnail and contains an audio meter and a gain slider that goes from -6dB to 6dB with the default at 0 and a soft snap to 0. Adjusting the gain should not change the visualisation of the track in the progress bar. 
        - note: when there is no audio loaded, the contents of tab 2 "Playback" should be grayed out
    - tab 3: "Sessions" Move the Saved Sessions sidebar to a third tab. Include the Save/Delete buttons here
    - tab 4: "Camelot" Display a rendering of the Camelot Wheel
