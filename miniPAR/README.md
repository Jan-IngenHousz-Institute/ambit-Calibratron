# Mini PAR meter - minimum tool to get a good numerical sense of light intensity


![vis](./KiCad/miniPAR.png)



# Commands

* to be added
* "reboot" -> 
    - Response: series of lines with info. 
    - ends with the "Ready" line
* "hello" -> 
    - Response: "MiniPAR,V" + version
* "spec" ->
    - Response: {"spectrometer":{"model":"AS7341","channels":{"f1_415":117,"f2_445":602,"f3_480":732,"f4_515":839,"f5_555":1606,"f6_590":1711,"f7_630":1765,"f8_680":1041,"clear":3704,"nir":359}}}
* "set_led"+ current value in mA ->
    - Response:  {"spectrometer":{"led_current_ma":20}} 
    - Example for 20mA
* "par" -> 
    - Response: 123.45
    - Read back par vale. This is a par_raw scaled by y=ax+b
* "par_raw"
    - Response: 1.23 
    - The weighted sum of the channel counts
* "cal_par_slope," + slope -> 
    - Response: 10.0
    - Example for slope = 10.0
    - Set the "a" coefficient for y=ax+b scaling
* "cal_par_intercept," + intercept -> 
    - Response: 2.3
    - Example for intercept = 2.3
    - Set the "b" coefficient for y=ax+b scaling
* "set_name," + string
    - response: "new_name: Lukasz"
    - add custom device name
* "get_name" 
    - response: "Lukasz"
    

# Protocol 

* terminator: "\r"
* PAR uinits - µmol/m²/s"


FW Design (L. Caracciolo),

HW Design (L. Grabowski)
