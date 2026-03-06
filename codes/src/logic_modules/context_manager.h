#ifndef CONTEXT_MANAGER_H
#define CONTEXT_MANAGER_H

#include "../common/common.h"
class ContextManager
{
	public:
	static int get_battery_level()
	{
		return 85;
	}
	static bool is_wifi_connected() 
	{ 
	        return true; 
	}
	static int get_signal_dbm()
	{
		return -90;
	}
	static Coin select_coin()
	{
		int bat=get_battery_level();
		int sigl=get_signal_dbm();
		if(bat<5)
			return BRONZE;
		if(sigl<-80)
			return SILVER;
		return SILVER;
	}
	static bool is_ideal_state()
	{
		return (get_battery_level()>20 && is_wifi_connected());
	}
};

#endif
